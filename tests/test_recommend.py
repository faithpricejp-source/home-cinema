"""推荐测试：假 HTTP 客户端 + 虚构片名。覆盖权重、加总、过滤、缓存、API。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from homecinema.db import utcnow
from homecinema.metadata import TmdbClient
from homecinema.player import Player
from homecinema.recommend import generate, movie_weight, show_weight
from homecinema.server import RecService, ScanService, create_app


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, content=b""):
        self.status_code = status_code
        self._json = json_data
        self.content = content

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


class FakeHttp:
    """路由：url 含子串 -> list（当作 results）或 FakeResponse。"""

    def __init__(self):
        self.routes = []
        self.calls = []

    def add(self, substr, responder):
        self.routes.append((substr, responder))

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": dict(params or {})})
        for substr, responder in self.routes:
            if substr in url:
                if isinstance(responder, FakeResponse):
                    return responder
                return FakeResponse(200, {"results": responder})
        raise AssertionError("unexpected GET " + url)


def make_tmdb():
    http = FakeHttp()
    client = TmdbClient("test-key", "zh-CN", http=http, sleep=lambda _s: None,
                        clock=lambda: 0.0, min_interval=0.0)
    return client, http


def movie_cand(cid, title="Cand", vote_count=500, vote_average=7.5, adult=False):
    return {"id": cid, "title": title, "original_title": title,
            "release_date": "2015-01-01", "overview": "简介" + str(cid),
            "poster_path": "/p%d.jpg" % cid, "vote_count": vote_count,
            "vote_average": vote_average, "adult": adult}


def tv_cand(cid, title="Show", vote_count=500, vote_average=7.5, adult=False):
    return {"id": cid, "name": title, "original_name": title,
            "first_air_date": "2016-01-01", "overview": "简介" + str(cid),
            "poster_path": "/t%d.jpg" % cid, "vote_count": vote_count,
            "vote_average": vote_average, "adult": adult}


def add_movie(lib, name, tmdb_id=None, watched=False, position=None, duration=None):
    mid = lib.upsert_movie(path="/movies/" + name, folder="/movies/" + name,
                           video_name=name + ".mp4", ext="mp4", title=name,
                           year=2000, size=0, mtime=0.0)
    if tmdb_id is not None:
        lib.update_movie_metadata(mid, {"tmdb_id": tmdb_id, "status": "matched"})
    if watched or position is not None:
        lib.save_playback("movie", mid, float(position or 0.0), duration, watched)
    return mid


def add_show(lib, name, tmdb_id, watched_episodes=0):
    sid = lib.upsert_show(path="/tv/" + name, folder="/tv/" + name, title=name, year=2010)
    lib.update_show_metadata(sid, {"tmdb_id": tmdb_id, "status": "matched"})
    for i in range(max(watched_episodes, 1)):
        eid = lib.upsert_episode(show_id=sid, season_number=1, episode_number=i + 1,
                                 path="/tv/%s/S01E%02d.mkv" % (name, i + 1),
                                 video_name="ep.mkv", ext="mkv", title="ep",
                                 size=0, mtime=0.0)
        if i < watched_episodes:
            lib.save_playback("episode", eid, 100.0, 1000.0, True)
    return sid


# ---------- 权重 ----------

def test_movie_weights_order():
    assert movie_weight({"watched": 1, "position_sec": 1000,
                         "duration_sec": 1000, "played_at": utcnow()}) == 3.0
    assert movie_weight({"watched": 0, "position_sec": 500,
                         "duration_sec": 1000, "played_at": utcnow()}) == 2.0
    assert movie_weight({"watched": 0, "position_sec": 0,
                         "duration_sec": None, "played_at": None}) == 1.0
    old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    assert movie_weight({"watched": 0, "position_sec": 40,
                         "duration_sec": 1000, "played_at": old}) == 0.3
    # 已看 > 在看 > 没看 > 放弃
    assert 3.0 > 2.0 > 1.0 > 0.3


def test_movie_abandoned_requires_old_and_low_progress():
    recent = utcnow()
    # 进度 <10%、≥30 秒，但刚刚播过 → 不算放弃
    assert movie_weight({"watched": 0, "position_sec": 40,
                         "duration_sec": 1000, "played_at": recent}) == 1.0
    old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    # 进度 ≥10% → 不算放弃
    assert movie_weight({"watched": 0, "position_sec": 200,
                         "duration_sec": 1000, "played_at": old}) == 1.0


def test_show_weights():
    assert show_weight(3) == 3.0 and show_weight(6) == 3.0
    assert show_weight(1) == 2.0 and show_weight(2) == 2.0
    assert show_weight(0) == 1.0


# ---------- 打分 ----------

def test_multi_source_accumulate_and_rank_decay(cfg, lib):
    add_movie(lib, "Source A", tmdb_id=501, watched=True)  # 权重 3
    add_movie(lib, "Source B", tmdb_id=502)                # 权重 1
    client, http = make_tmdb()
    http.add("/movie/501/recommendations", [movie_cand(900, "X"), movie_cand(901, "Y")])
    http.add("/movie/502/recommendations", [movie_cand(901, "Y"), movie_cand(902, "Z")])
    http.add("image.tmdb.org", FakeResponse(200, content=b"img"))

    stats = generate(lib, client, cfg.cache_dir_path, cfg.images_dir)
    assert stats["sources"] == 2
    rows = {r["tmdb_id"]: r for r in lib.list_recommendations()}
    assert rows[900]["score"] == pytest.approx(3.0)
    assert rows[901]["score"] == pytest.approx(3 * (1 - 1 / 40) + 1 * (1 - 0 / 40))
    assert rows[902]["score"] == pytest.approx(1 * (1 - 1 / 40))
    assert rows[901]["score"] > rows[900]["score"] > rows[902]["score"]


def test_because_top_three_sources(cfg, lib):
    add_movie(lib, "Alpha Watched", tmdb_id=501, watched=True)               # 3
    add_movie(lib, "Beta Progress", tmdb_id=502, position=500, duration=1000)  # 2
    add_movie(lib, "Gamma Fresh", tmdb_id=503)                                # 1
    add_movie(lib, "Delta Fresh", tmdb_id=504)                                # 1
    client, http = make_tmdb()
    for sid in (501, 502, 503, 504):
        http.add("/movie/%d/recommendations" % sid, [movie_cand(900, "Target")])
    http.add("image.tmdb.org", FakeResponse(200, content=b"img"))

    generate(lib, client, cfg.cache_dir_path, cfg.images_dir)
    row = lib.list_recommendations()[0]
    assert json.loads(row["because"]) == ["Alpha Watched", "Beta Progress", "Gamma Fresh"]


# ---------- 过滤 ----------

def test_owned_including_missing_excluded(cfg, lib):
    add_movie(lib, "Source", tmdb_id=501)
    add_movie(lib, "Owned Missing", tmdb_id=901)
    lib.mark_missing_movies({"/movies/Source"})  # 只有 901 变 missing=1

    client, http = make_tmdb()
    http.add("/movie/501/recommendations", [movie_cand(900, "X"), movie_cand(901, "Y")])
    http.add("image.tmdb.org", FakeResponse(200, content=b"img"))
    generate(lib, client, cfg.cache_dir_path, cfg.images_dir)

    ids = {r["tmdb_id"] for r in lib.list_recommendations()}
    assert ids == {900}  # 已拥有的 901 被排除，包括 missing 的


def test_rating_threshold(cfg, lib):
    add_movie(lib, "Source", tmdb_id=501)
    client, http = make_tmdb()
    http.add("/movie/501/recommendations", [
        movie_cand(900, "LowVotes", vote_count=149),
        movie_cand(901, "LowRating", vote_average=6.4),
        movie_cand(902, "Adult", adult=True),
        movie_cand(903, "Good"),
    ])
    http.add("image.tmdb.org", FakeResponse(200, content=b"img"))
    generate(lib, client, cfg.cache_dir_path, cfg.images_dir)

    ids = {r["tmdb_id"] for r in lib.list_recommendations()}
    assert ids == {903}


def test_dismissed_excluded(cfg, lib):
    add_movie(lib, "Source", tmdb_id=501)
    lib.dismiss_recommendation("movie", 900)
    client, http = make_tmdb()
    http.add("/movie/501/recommendations", [movie_cand(900, "X"), movie_cand(901, "Y")])
    http.add("image.tmdb.org", FakeResponse(200, content=b"img"))
    generate(lib, client, cfg.cache_dir_path, cfg.images_dir)

    ids = {r["tmdb_id"] for r in lib.list_recommendations()}
    assert ids == {901}


# ---------- 缓存 ----------

def test_cache_reuse_and_refresh(cfg, lib):
    add_movie(lib, "Source", tmdb_id=501)
    client, http = make_tmdb()
    http.add("/movie/501/recommendations", [movie_cand(900, "X")])
    http.add("image.tmdb.org", FakeResponse(200, content=b"img"))

    def rec_calls():
        return [c for c in http.calls if "/movie/501/recommendations" in c["url"]]

    generate(lib, client, cfg.cache_dir_path, cfg.images_dir)
    assert len(rec_calls()) == 1
    assert (cfg.cache_dir_path / "recs" / "movie_501.json").is_file()

    generate(lib, client, cfg.cache_dir_path, cfg.images_dir)  # 7 天内不重拉
    assert len(rec_calls()) == 1

    generate(lib, client, cfg.cache_dir_path, cfg.images_dir, refresh=True)  # 强制重拉
    assert len(rec_calls()) == 2


# ---------- 电影/剧集分开 ----------

def test_movies_and_shows_summarized_separately(cfg, lib):
    add_movie(lib, "Film Source", tmdb_id=501, watched=True)
    add_show(lib, "Show Source", tmdb_id=601, watched_episodes=3)
    client, http = make_tmdb()
    http.add("/movie/501/recommendations", [movie_cand(900, "Film Rec")])
    http.add("/tv/601/recommendations", [tv_cand(950, "Show Rec")])
    http.add("image.tmdb.org", FakeResponse(200, content=b"img"))

    stats = generate(lib, client, cfg.cache_dir_path, cfg.images_dir)
    assert stats == {"movies": 1, "shows": 1, "sources": 2}
    by_kind = {r["kind"]: r for r in lib.list_recommendations()}
    assert by_kind["movie"]["tmdb_id"] == 900
    assert by_kind["tv"]["tmdb_id"] == 950
    assert by_kind["tv"]["title"] == "Show Rec"


# ---------- API ----------

def seed_rec_rows(lib):
    now = utcnow()
    lib.save_recommendations([
        {"kind": "movie", "tmdb_id": 900, "title": "Film Rec", "original_title": "Film Rec",
         "year": 2015, "overview": "o", "poster_cached": None, "vote_average": 7.53,
         "vote_count": 500, "score": 3.0, "because": json.dumps(["Source A", "Source B"]),
         "generated_at": now},
        {"kind": "tv", "tmdb_id": 950, "title": "Show Rec", "original_title": "Show Rec",
         "year": 2016, "overview": "o", "poster_cached": None, "vote_average": 8.0,
         "vote_count": 400, "score": 2.0, "because": json.dumps(["Show Source"]),
         "generated_at": now},
    ])


@pytest.fixture
def rec_client(cfg, lib):
    seed_rec_rows(lib)
    player = Player(lib, cfg, launcher=lambda *a, **k: None,
                    poll_interval=0.05, connect_deadline=1.0)
    app = create_app(config=cfg, db=lib, player=player,
                     scan_service=ScanService(cfg, lib), rec_service=RecService(cfg, lib))
    with TestClient(app) as tc:
        yield tc


def test_api_recommendations_list(rec_client):
    data = rec_client.get("/api/recommendations").json()
    assert set(data) == {"movies", "shows", "generated_at"}
    assert data["generated_at"]
    movie = data["movies"][0]
    assert movie["kind"] == "movie" and movie["tmdb_id"] == 900
    assert movie["rating"] == 7.5
    assert movie["because"] == ["Source A", "Source B"]
    assert movie["url"] == "https://www.themoviedb.org/movie/900"
    assert data["shows"][0]["url"] == "https://www.themoviedb.org/tv/950"


def test_api_dismiss(rec_client, lib):
    resp = rec_client.post("/api/recommendations/dismiss",
                           json={"kind": "movie", "tmdb_id": 900})
    assert resp.status_code == 200
    data = rec_client.get("/api/recommendations").json()
    assert all(m["tmdb_id"] != 900 for m in data["movies"])
    assert ("movie", 900) in lib.dismissed_recommendations()


def test_api_refresh_without_key_409(rec_client):
    resp = rec_client.post("/api/recommendations/refresh")
    assert resp.status_code == 409
    assert "TMDB" in resp.json()["detail"]


def test_api_recommendations_status(rec_client):
    st = rec_client.get("/api/recommendations/status").json()
    assert st["running"] is False and st["phase"] == "idle"
