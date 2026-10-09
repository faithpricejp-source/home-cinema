"""API 测试：FastAPI TestClient 打每个端点至少一次。"""

from __future__ import annotations

import os
import subprocess
import time

import pytest
from fastapi.testclient import TestClient

from conftest import wait_until

from homecinema.player import Player
from homecinema.scanner import run_scan
from homecinema.server import ScanService, create_app


@pytest.fixture
def client(tmp_path, cfg, lib):
    # 造假片库：1 部电影 + 1 部剧 2 集（虚构名）
    movies = tmp_path / "movies"
    folder = movies / "Example Movie (2001)"
    folder.mkdir(parents=True)
    (folder / "Example Movie (2001).mp4").write_bytes(b"")
    tv = tmp_path / "tv"
    show = tv / "Sample Show"
    (show / "Season 01").mkdir(parents=True)
    (show / "Season 01" / "Sample Show S01E01.mkv").write_bytes(b"")
    (show / "Season 01" / "Sample Show S01E02.mkv").write_bytes(b"")
    # 电影文件比剧集早入库：显式拨回 mtime（macOS 会连 birthtime 一起拨回）。
    # Linux ext4 时间戳按内核 tick 取整，连写的几个文件 mtime 完全相同，不拨就并列、顺序看运气
    old = time.time() - 3600
    os.utime(folder / "Example Movie (2001).mp4", (old, old))

    run_scan(cfg, lib)

    captured = {}

    def launcher(cmd, stdin=None, stdout=None, stderr=None):
        captured["cmd"] = list(cmd)
        captured["stdin"] = stdin

    player = Player(lib, cfg, launcher=launcher, poll_interval=0.05, connect_deadline=1.0)
    app = create_app(config=cfg, db=lib, player=player, scan_service=ScanService(cfg, lib))
    with TestClient(app) as tc:
        tc.captured = captured  # type: ignore[attr-defined]
        yield tc


def test_index_serves_html(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "HomeCinema" in resp.text
    assert "app.css?v=" in resp.text and "app.js?v=" in resp.text


def test_static_served(client):
    resp = client.get("/static/app.css")
    assert resp.status_code == 200
    assert "--accent" in resp.text
    resp = client.get("/static/app.js")
    assert resp.status_code == 200


def test_home_payload(client):
    data = client.get("/api/home").json()
    assert set(data) == {"continue_watching", "recently_added", "movies", "shows"}
    assert len(data["movies"]) == 1
    assert data["movies"][0]["title"] == "Example Movie"
    assert data["movies"][0]["status"] == "unmatched"  # 无 key，不拉元数据
    assert len(data["shows"]) == 1
    assert data["shows"][0]["episode_count"] == 2
    # 电影一张、剧集一张（不按单集刷屏）
    assert [r["type"] for r in data["recently_added"]] == ["show", "movie"]
    assert data["continue_watching"] == []


def test_movies_list_search_and_sort(client):
    assert len(client.get("/api/movies").json()) == 1
    assert len(client.get("/api/movies", params={"q": "example"}).json()) == 1
    assert client.get("/api/movies", params={"q": "zzz"}).json() == []
    for sort in ("added", "title", "year"):
        assert client.get("/api/movies", params={"sort": sort}).status_code == 200
    resp = client.get("/api/shows", params={"q": "sample", "sort": "added"})
    assert resp.status_code == 200 and len(resp.json()) == 1


def test_movie_detail(client):
    movie = client.get("/api/movies").json()[0]
    detail = client.get(f"/api/movie/{movie['id']}").json()
    assert detail["title"] == "Example Movie"
    assert detail["year"] == 2001
    assert detail["runtime_minutes"] is None
    assert detail["position_sec"] is None and detail["watched"] is False
    assert client.get("/api/movie/9999").status_code == 404


def test_show_detail_with_seasons(client):
    show = client.get("/api/shows").json()[0]
    detail = client.get(f"/api/show/{show['id']}").json()
    assert detail["title"] == "Sample Show"
    assert len(detail["seasons"]) == 1
    season = detail["seasons"][0]
    assert [(e["season_number"], e["episode_number"]) for e in season["episodes"]] == \
        [(1, 1), (1, 2)]
    assert detail["next_episode"]["episode_number"] == 1  # 没看过 → 第一集
    assert client.get("/api/show/9999").status_code == 404


def test_play_movie(client):
    movie = client.get("/api/movies").json()[0]
    resp = client.post("/api/play", json={"type": "movie", "id": movie["id"]})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "start_at": 0}
    cmd = client.captured["cmd"]
    assert "--no-stdin" in cmd
    assert client.captured["stdin"] == subprocess.DEVNULL


def test_play_errors(client):
    assert client.post("/api/play", json={"type": "movie", "id": 9999}).status_code == 404
    assert client.post("/api/play", json={"type": "person", "id": 1}).status_code == 422


def test_watched_toggle(client):
    movie = client.get("/api/movies").json()[0]
    resp = client.post("/api/watched",
                       json={"type": "movie", "id": movie["id"], "watched": True})
    assert resp.status_code == 200 and resp.json() == {"ok": True}
    assert client.get(f"/api/movie/{movie['id']}").json()["watched"] is True
    client.post("/api/watched", json={"type": "movie", "id": movie["id"], "watched": False})
    assert client.get(f"/api/movie/{movie['id']}").json()["watched"] is False
    assert client.post("/api/watched",
                       json={"type": "movie", "id": 9999, "watched": True}).status_code == 404


def test_play_episode_and_continue_watching(client):
    show = client.get("/api/shows").json()[0]
    detail = client.get(f"/api/show/{show['id']}").json()
    ep1 = detail["seasons"][0]["episodes"][0]
    resp = client.post("/api/play", json={"type": "episode", "id": ep1["id"]})
    assert resp.status_code == 200
    # 播了但没看完（启动即停，位置 <30 秒）→ 不进「继续观看」
    assert client.get("/api/home").json()["continue_watching"] == []
    # 手动给进度：E01 看完 → 推 E02
    client.post("/api/watched", json={"type": "episode", "id": ep1["id"], "watched": True})
    cards = client.get("/api/home").json()["continue_watching"]
    assert len(cards) == 1
    assert cards[0]["type"] == "episode"
    assert cards[0]["episode_number"] == 2
    assert cards[0]["show_title"] == "Sample Show"
    # 详情页的下一集也变成 E02
    detail = client.get(f"/api/show/{show['id']}").json()
    assert detail["next_episode"]["episode_number"] == 2


def test_scan_endpoints(client):
    resp = client.post("/api/scan")
    assert resp.status_code == 200 and resp.json()["ok"] is True
    status = wait_until(lambda: (lambda s: s if not s["running"] else None)(
        client.get("/api/scan/status").json()), timeout=10)
    assert status["phase"] == "idle"
    assert status["movies"] == 1 and status["episodes"] == 2
    assert status["error"] is None  # 无 key → 只扫描不拉元数据
    assert status["metadata_total"] == 0


def test_img_endpoint(client, cfg):
    images = cfg.images_dir
    images.mkdir(parents=True, exist_ok=True)
    (images / "abc.jpg").write_bytes(b"\xff\xd8jpegdata")
    resp = client.get("/img/abc.jpg")
    assert resp.status_code == 200
    assert resp.content == b"\xff\xd8jpegdata"
    assert client.get("/img/missing.jpg").status_code == 404
    assert client.get("/img/..%2Fetc%2Fpasswd").status_code == 404  # 穿越拒绝
    assert client.get("/img/sub%2Fabc.jpg").status_code == 404
