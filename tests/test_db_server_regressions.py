"""回归：2026-10-03 ZCode GLM-5.3-Flash 审计 db.py/server.py 找到的 4 个 bug（F01–F04）。

每个测试对应一条发现，断言正确行为（修复前全部失败）。
测试断言的是正确行为，因此在当前代码上应当失败。
一律虚构片名，不访问外网、不启动播放器。
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from homecinema.db import Library
from homecinema.player import Player
from homecinema.scanner import run_scan
from homecinema.server import ScanService, create_app


# ---------- 复用全局 fixture：cfg / lib（见 tests/conftest.py） ----------

def seed_show(lib: Library) -> tuple[int, dict]:
    show_id = lib.upsert_show(path="/lib-fixture/Sample Show",
                              folder="/lib-fixture/Sample Show",
                              title="Sample Show", year=2001)
    eps = {}
    for season, episode in [(1, 1), (1, 2), (1, 3)]:
        eps[(season, episode)] = lib.upsert_episode(
            show_id=show_id, season_number=season, episode_number=episode,
            path=f"/lib-fixture/Sample Show/S{season:02d}E{episode:02d}.mkv",
            video_name=f"S{season:02d}E{episode:02d}.mkv", ext="mkv",
            title=f"Sample Show S{season:02d}E{episode:02d}", size=1, mtime=1.0)
    return show_id, eps


def seed_movie(lib: Library, title="Example Movie",
               path="/lib-fixture/Example Movie (2001).mp4") -> int:
    return lib.upsert_movie(path=path, folder="/lib-fixture",
                            video_name=path.rsplit("/", 1)[-1], ext="mp4",
                            title=title, year=2001, size=1, mtime=1.0)


# ---------- F01 ----------

def test_F01_continue_watching_next_card_sorted_by_show_activity(lib):
    show_id, eps = seed_show(lib)
    movie_id = seed_movie(lib)
    # 先播电影（较早），后看完剧集 E01（更晚）→ 剧集的「继续观看」卡应排在前
    lib.save_playback("movie", movie_id, 600, 1200, False)
    time.sleep(0.02)
    lib.save_playback("episode", eps[(1, 1)], 1296, 1300, True)
    cards = lib.continue_watching()
    types = [c["type"] for c in cards]
    ep_card = next(c for c in cards if c["type"] == "episode")
    # 剧集卡排在前（最近看的是剧集），且其 updated_at 应反映剧集最近一次观看时间
    assert types.index("episode") < types.index("movie")
    assert ep_card["updated_at"] is not None


# ---------- F02 ----------

@pytest.fixture
def client(tmp_path, cfg, lib):
    folder = tmp_path / "movies" / "Example Movie (2001)"
    folder.mkdir(parents=True)
    (folder / "Example Movie (2001).mp4").write_bytes(b"")
    show = tmp_path / "tv" / "Sample Show" / "Season 01"
    show.mkdir(parents=True)
    (show / "Sample Show S01E01.mkv").write_bytes(b"")
    (show / "Sample Show S01E02.mkv").write_bytes(b"")
    run_scan(cfg, lib)
    player = Player(lib, cfg, launcher=lambda *a, **k: None)
    app = create_app(config=cfg, db=lib, player=player,
                     scan_service=ScanService(cfg, lib))
    with TestClient(app) as tc:
        yield tc


def test_F02_show_detail_episode_count_matches_available_episodes(client):
    show = client.get("/api/shows").json()[0]
    detail = client.get(f"/api/show/{show['id']}").json()
    assert detail["episode_count"] == 2
    assert detail["watched_count"] == 0
    ep_id = detail["seasons"][0]["episodes"][0]["id"]
    assert client.post("/api/watched", json={"type": "episode", "id": ep_id, "watched": True}).status_code == 200
    detail = client.get(f"/api/show/{show['id']}").json()
    assert (detail["episode_count"], detail["watched_count"]) == (2, 1)


# ---------- F03 ----------

def test_F03_img_null_byte_returns_404_not_500(tmp_path, cfg, lib):
    cfg.images_dir.mkdir(parents=True, exist_ok=True)
    (cfg.images_dir / "abc.jpg").write_bytes(b"jpg")
    player = Player(lib, cfg, launcher=lambda *a, **k: None)
    app = create_app(config=cfg, db=lib, player=player,
                     scan_service=ScanService(cfg, lib))
    # raise_server_exceptions=False 才能看到真实客户端拿到的 500 响应
    with TestClient(app, raise_server_exceptions=False) as tc:
        assert tc.get("/img/abc.jpg").status_code == 200
        assert tc.get("/img/abc%00.jpg").status_code == 404


# ---------- F04 ----------

def test_F04_watched_invalid_type_returns_422(client):
    resp = client.post("/api/watched",
                       json={"type": "person", "id": 1, "watched": True})
    assert resp.status_code == 422
    assert client.post("/api/watched",
                       json={"type": "", "id": 1, "watched": True}).status_code == 422
