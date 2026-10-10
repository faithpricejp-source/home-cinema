"""同片多版本合并：tmdb_id 分组、默认播放版本、继续观看去重、详情页版本列表、旧库升级。

一律用虚构片名；数据直接 upsert 进临时库，不走扫描。"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from homecinema.db import Library, _version_label
from homecinema.player import Player
from homecinema.server import ScanService, create_app


def seed_movie(lib: Library, folder_name: str, video_name: str, title: str, year=2009,
               ext="mp4", size=1000, tmdb=None, height=None):
    folder = f"/lib-fixture/{folder_name}"
    mid = lib.upsert_movie(path=f"{folder}/{video_name}", folder=folder,
                           video_name=video_name, ext=ext, title=title, year=year,
                           size=size, mtime=1.0)
    if height is not None:
        lib.set_video_height(mid, height)
    if tmdb is not None:
        lib.update_movie_metadata(mid, {"tmdb_id": tmdb, "status": "matched"})
    return mid


@pytest.fixture
def lib2(lib: Library):
    """Watchmen 两个版本（tmdb 13183）+ Black Mask 两个版本（tmdb 9460）+ 未匹配一部。"""
    a = seed_movie(lib, "Watchmen (2009)", "Watchmen (2009).mp4", "Watchmen",
                   size=2000, tmdb=13183, height=1080)
    b = seed_movie(lib, "Watchmen (2009) - Ultimate Cut",
                   "Watchmen (2009) - Ultimate Cut.mp4", "Watchmen",
                   size=4000, tmdb=13183, height=1080)
    m1 = seed_movie(lib, "Black Mask (1996)", "Black Mask (1996).mp4", "Black Mask",
                    year=1996, size=1000, tmdb=9460, height=720)
    m2 = seed_movie(lib, "Black Mask (1996)", "Black Mask (1996).mkv", "Black Mask",
                    year=1996, ext="mkv", size=1500, tmdb=9460, height=1080)
    solo = seed_movie(lib, "Odd One Out (1980)", "Odd One Out (1980).mkv",
                      "Odd One Out", year=1980, ext="mkv")
    return {"a": a, "b": b, "m1": m1, "m2": m2, "solo": solo}


def reps(lib):
    return {g["row"]["title"]: (g["row"]["id"], g["count"]) for g in lib.list_movie_groups()}


# ---------- 分组与版本数 ----------

def test_group_dedup_and_count(lib, lib2):
    groups = lib.list_movie_groups()
    assert len(groups) == 3  # Watchmen、Black Mask、未匹配各自
    by_title = reps(lib)
    assert by_title["Watchmen"] == (lib2["b"], 2)  # 默认=未播过里文件更大的
    assert by_title["Black Mask"] == (lib2["m2"], 2)  # 默认=分辨率高的 mkv
    assert by_title["Odd One Out"] == (lib2["solo"], 1)  # 未匹配仍各自一张


def test_unmatched_files_each_keep_own_card(lib):
    s1 = seed_movie(lib, "No Tmdb One (2000)", "No Tmdb One (2000).mp4", "No Tmdb One")
    s2 = seed_movie(lib, "No Tmdb Two (2001)", "No Tmdb Two (2001).mp4", "No Tmdb Two")
    ids = {g["row"]["id"] for g in lib.list_movie_groups()}
    assert {s1, s2} <= ids
    assert all(g["count"] == 1 for g in lib.list_movie_groups() if g["row"]["tmdb_id"] is None)


def test_unfavorite_from_other_version_clears_movie_group(cfg, lib, lib2):
    lib.set_favorite("movie", lib2["a"], True)
    with TestClient(create_app(config=cfg, db=lib), base_url="http://127.0.0.1") as client:
        assert client.get(f'/api/movie/{lib2["b"]}').json()["favorite"] is True
        response = client.post("/api/favorite", json={"type": "movie", "id": lib2["b"], "favorite": False})
        assert response.status_code == 200
        assert client.get(f'/api/movie/{lib2["b"]}').json()["favorite"] is False
        assert client.get('/api/movies?fav=true').json() == []


def test_search_and_sort_dedup(lib, lib2):
    groups = lib.list_movie_groups(q="watchmen")
    assert [(g["row"]["id"], g["count"]) for g in groups] == [(lib2["b"], 2)]
    ordered = [g["row"]["title"] for g in lib.list_movie_groups(sort="year")]
    assert ordered == ["Watchmen", "Black Mask", "Odd One Out"]  # 年份新→旧，同 tmdb 不重复
    by_title = [g["row"]["title"] for g in lib.list_movie_groups(sort="title")]
    assert sorted(by_title) == ["Black Mask", "Odd One Out", "Watchmen"]
    assert len(by_title) == 3


# ---------- 默认播放版本 ----------

def test_default_version_recently_played_wins(lib, lib2):
    lib.save_playback("movie", lib2["a"], 600, 6000, False)
    assert reps(lib)["Watchmen"][0] == lib2["a"]
    lib.save_playback("movie", lib2["b"], 100, 6000, False)  # b 播放时间更新
    assert reps(lib)["Watchmen"][0] == lib2["b"]
    # 播过（哪怕标成已看、进度 0）仍优先于没播过的高规格
    lib.save_playback("movie", lib2["a"], 0, 6000, True)
    assert reps(lib)["Watchmen"][0] == lib2["a"]


def test_default_version_height_then_size(lib, lib2):
    lib.set_video_height(lib2["a"], 2160)  # 没播过的 4K 版本优先于 1080
    assert reps(lib)["Watchmen"][0] == lib2["a"]
    lib.set_video_height(lib2["b"], 2160)  # 分辨率并列取文件大的（b=4000 > a=2000）
    assert reps(lib)["Watchmen"][0] == lib2["b"]
    lib.save_playback("movie", lib2["a"], 600, 6000, False)  # 播过 again 反超规格
    assert reps(lib)["Watchmen"][0] == lib2["a"]


# ---------- 版本名与版本列表 ----------

def test_version_label_suffix_resolution_ext(lib, lib2):
    labels = {v["id"]: _version_label(v["folder"], v["video_name"], v["title"], v["ext"],
                                      v["video_height"])
              for v in lib.movie_versions(lib2["a"])}
    assert labels[lib2["b"]] == "Ultimate Cut"   # 文件夹/文件名里年份之后的后缀
    assert labels[lib2["a"]] == "1080p · mp4"    # 没后缀 → 分辨率 · 扩展名
    no_h = seed_movie(lib, "No Height (1999)", "No Height (1999).avi", "No Height", ext="avi")
    assert _version_label("/lib-fixture/No Height (1999)", "No Height (1999).avi",
                          "No Height", "avi", None) == "avi"


def test_movie_versions_lists_same_tmdb_only_present(lib, lib2):
    ids = {v["id"] for v in lib.movie_versions(lib2["a"])}
    assert ids == {lib2["a"], lib2["b"]}
    assert {v["id"] for v in lib.movie_versions(lib2["solo"])} == {lib2["solo"]}
    # 文件消失（missing）的版本不进列表
    keep = {lib.get_movie(x)["path"] for x in (lib2["b"], lib2["m1"], lib2["m2"], lib2["solo"])}
    lib.mark_missing_movies(keep)
    ids = {v["id"] for v in lib.movie_versions(lib2["b"])}
    assert lib2["a"] not in ids


# ---------- 继续观看去重 ----------

def test_continue_watching_one_card_latest_version(lib, lib2):
    lib.save_playback("movie", lib2["a"], 600, 6000, False)
    lib.save_playback("movie", lib2["b"], 900, 6000, False)  # 更晚播放
    lib.save_playback("movie", lib2["solo"], 60, 600, False)
    cards = lib.continue_watching()
    movie_cards = [c for c in cards if c["type"] == "movie"]
    titles = sorted(c["title"] for c in movie_cards)
    assert titles == ["Odd One Out", "Watchmen"]
    watchmen = [c for c in movie_cards if c["title"] == "Watchmen"][0]
    assert watchmen["id"] == lib2["b"]  # 取最近播放的那个版本
    assert watchmen["position_sec"] == 900


def test_continue_watching_watched_version_not_merged_with_other(lib, lib2):
    # 进度按文件各自记：a 看完、b 没看过 → 继续观看两张卡都不出现
    lib.save_playback("movie", lib2["a"], 6000, 6000, True)
    assert [c for c in lib.continue_watching() if c["type"] == "movie"] == []


# ---------- API ----------

@pytest.fixture
def client(cfg, lib, lib2):
    def launcher(cmd, stdin=None, stdout=None, stderr=None):
        pass

    player = Player(lib, cfg, launcher=launcher, poll_interval=0.05, connect_deadline=1.0)
    app = create_app(config=cfg, db=lib, player=player, scan_service=ScanService(cfg, lib))
    with TestClient(app, base_url="http://127.0.0.1") as tc:
        yield tc


def test_api_movies_home_dedup_badge(client, lib2):
    items = client.get("/api/movies", params={"q": "watchmen"}).json()
    assert len(items) == 1 and items[0]["version_count"] == 2
    assert items[0]["id"] == lib2["b"]
    home = client.get("/api/home").json()
    assert sum(m["version_count"] for m in home["movies"]) == 5  # 2+2+1，三张卡
    assert len(home["movies"]) == 3
    ra = [r for r in home["recently_added"] if r["type"] == "movie"]
    assert len(ra) == 3
    assert {r["id"] for r in ra} == {lib2["b"], lib2["m2"], lib2["solo"]}


def test_api_movie_detail_versions_and_default(client, lib2):
    detail = client.get(f"/api/movie/{lib2['a']}").json()
    assert detail["default_version_id"] == lib2["b"]
    assert detail["version_count"] == 2
    by_id = {v["id"]: v for v in detail["versions"]}
    assert set(by_id) == {lib2["a"], lib2["b"]}
    assert by_id[lib2["b"]]["name"] == "Ultimate Cut"
    assert by_id[lib2["b"]]["is_default"] and by_id[lib2["b"]]["size"] == 4000
    assert by_id[lib2["a"]]["video_height"] == 1080
    assert "progress" in by_id[lib2["a"]] and "played_at" in by_id[lib2["a"]]


def test_api_detail_progress_still_per_file(client, lib2):
    client.post("/api/watched", json={"type": "movie", "id": lib2["a"], "watched": True})
    d = client.get(f"/api/movie/{lib2['a']}").json()
    assert d["watched"] is True
    by_id = {v["id"]: v for v in d["versions"]}
    assert by_id[lib2["a"]]["watched"] is True
    assert by_id[lib2["b"]]["watched"] is False


# ---------- 旧库升级 ----------

OLD_MOVIES = """
CREATE TABLE movies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL UNIQUE,
    folder TEXT NOT NULL,
    video_name TEXT NOT NULL,
    ext TEXT NOT NULL,
    title TEXT NOT NULL,
    year INTEGER,
    size INTEGER NOT NULL DEFAULT 0,
    mtime REAL NOT NULL DEFAULT 0,
    added_at TEXT NOT NULL,
    updated_at TEXT,
    missing INTEGER NOT NULL DEFAULT 0,
    tmdb_id INTEGER,
    overview TEXT NOT NULL DEFAULT '',
    genres TEXT NOT NULL DEFAULT '[]',
    rating REAL,
    runtime_minutes INTEGER,
    poster_path TEXT,
    backdrop_path TEXT,
    poster_local TEXT,
    poster_cached TEXT,
    backdrop_cached TEXT,
    status TEXT NOT NULL DEFAULT 'unmatched',
    metadata_at TEXT
)
"""


def test_old_db_upgrades_and_groups(tmp_path):
    db_file = tmp_path / "old.db"
    conn = sqlite3.connect(db_file)
    conn.executescript(OLD_MOVIES)
    conn.execute("INSERT INTO movies (path, folder, video_name, ext, title, year, size, mtime, added_at, tmdb_id, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 ("/lib-fixture/Old Movie (2001)/Old Movie (2001).mp4",
                  "/lib-fixture/Old Movie (2001)", "Old Movie (2001).mp4", "mp4",
                  "Old Movie", 2001, 1000, 1.0, "2020-01-01T00:00:00+00:00", 555, "matched"))
    conn.execute("INSERT INTO movies (path, folder, video_name, ext, title, year, size, mtime, added_at, tmdb_id, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 ("/lib-fixture/Old Movie (2001) Director Cut/Old Movie (2001) Director Cut.mkv",
                  "/lib-fixture/Old Movie (2001) Director Cut", "Old Movie (2001) Director Cut.mkv", "mkv",
                  "Old Movie", 2001, 3000, 1.0, "2021-01-01T00:00:00+00:00", 555, "matched"))
    conn.commit()
    conn.close()

    lib = Library(db_file)  # 旧库打开：补 video_height/aka/collection_id 等新列
    lib.upsert_movie(path="/lib-fixture/Old Movie (2001)/Old Movie (2001).mp4",
                     folder="/lib-fixture/Old Movie (2001)",
                     video_name="Old Movie (2001).mp4", ext="mp4",
                     title="Old Movie", year=2001, size=1000, mtime=1.0)  # 重新入库不应崩
    lib.set_video_height(2, 2160)
    groups = lib.list_movie_groups()
    assert len(groups) == 1 and groups[0]["count"] == 2
    assert groups[0]["row"]["id"] == 2  # 默认=高分辨率那条
    assert groups[0]["added_at"] == "2021-01-01T00:00:00+00:00"  # 组内最新入库时间
    assert len(lib.movie_versions(1)) == 2
    lib.close()
