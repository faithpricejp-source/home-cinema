"""v2 内嵌播放器 API：/api/play 带 embedded、/api/progress、/api/next。

一律虚构片名；launcher 注入为记录调用的假函数，验证 embedded 路径不会启动 IINA。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from homecinema.player import Player, watched_flag
from homecinema.scanner import run_scan
from homecinema.server import ScanService, create_app


def build_tree(tmp_path, seasons=((1, 2),)):
    """造假片库：1 部电影 + 「Sample Show」，季数与每季集数由 seasons 给出。"""
    folder = tmp_path / "movies" / "Example Movie (2001)"
    folder.mkdir(parents=True)
    (folder / "Example Movie (2001).mp4").write_bytes(b"")
    show = tmp_path / "tv" / "Sample Show"
    for season_number, count in seasons:
        sdir = show / f"Season {season_number:02d}"
        sdir.mkdir(parents=True)
        for n in range(1, count + 1):
            (sdir / f"Sample Show S{season_number:02d}E{n:02d}.mkv").write_bytes(b"")


def make_client(tmp_path, cfg, lib):
    run_scan(cfg, lib)
    calls = []

    def launcher(cmd, stdin=None, stdout=None, stderr=None):
        calls.append(list(cmd))

    player = Player(lib, cfg, launcher=launcher, poll_interval=0.05, connect_deadline=1.0)
    app = create_app(config=cfg, db=lib, player=player, scan_service=ScanService(cfg, lib))
    tc = TestClient(app)
    tc.calls = calls  # type: ignore[attr-defined]
    tc.lib = lib  # type: ignore[attr-defined]
    tc.player = player  # type: ignore[attr-defined]
    return tc


@pytest.fixture
def client(tmp_path, cfg, lib):
    build_tree(tmp_path)
    with make_client(tmp_path, cfg, lib) as tc:
        yield tc


def get_movie_id(client) -> int:
    return client.get("/api/movies").json()[0]["id"]


def get_show_detail(client) -> dict:
    show_id = client.get("/api/shows").json()[0]["id"]
    return client.get(f"/api/show/{show_id}").json()


# ---------- /api/play 带 embedded ----------

def test_play_embedded_movie_returns_path_start_title(client):
    resp = client.post("/api/play",
                       json={"type": "movie", "id": get_movie_id(client), "embedded": True})
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert Path(data["path"]).is_absolute()
    assert data["path"].endswith("Example Movie (2001).mp4")
    assert data["start_at"] == 0
    assert data["title"] == "Example Movie"
    # 不启动 IINA、不起监控线程
    assert client.calls == []
    assert client.player.active_count() == 0


def test_play_embedded_episode_title_format(client):
    eps = get_show_detail(client)["seasons"][0]["episodes"]
    resp = client.post("/api/play",
                       json={"type": "episode", "id": eps[1]["id"], "embedded": True})
    assert resp.status_code == 200
    data = resp.json()
    assert data["title"] == "Sample Show · S01E02 · Sample Show S01E02"
    assert data["path"].endswith("Sample Show S01E02.mkv")
    assert client.calls == []


def test_play_embedded_resumes_from_last_position(client):
    movie_id = get_movie_id(client)
    client.lib.save_playback("movie", movie_id, 500, 1000, False)
    resp = client.post("/api/play",
                       json={"type": "movie", "id": movie_id, "embedded": True})
    assert resp.json()["start_at"] == 500.0


def test_play_embedded_missing_refused(client):
    movie_id = get_movie_id(client)
    client.lib._conn.execute("UPDATE movies SET missing=1")
    client.lib._conn.commit()
    resp = client.post("/api/play",
                       json={"type": "movie", "id": movie_id, "embedded": True})
    assert resp.status_code == 409
    assert client.calls == []


def test_play_without_embedded_still_launches_iina(client):
    """普通浏览器路径与 v1 完全一致：launcher 起 IINA + 返回结构不变。"""
    resp = client.post("/api/play",
                       json={"type": "movie", "id": get_movie_id(client)})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "start_at": 0}
    assert len(client.calls) == 1
    assert client.calls[0][0] == "/fake/iina-cli"


# ---------- /api/progress ----------

def test_progress_saves_position_and_duration(client):
    movie_id = get_movie_id(client)
    resp = client.post("/api/progress",
                       json={"type": "movie", "id": movie_id,
                             "position": 120, "duration": 1000})
    assert resp.status_code == 200 and resp.json() == {"ok": True}
    pb = client.lib.get_playback("movie", movie_id)
    assert pb["position_sec"] == 120
    assert pb["duration_sec"] == 1000
    assert pb["watched"] == 0


def test_progress_for_episode(client):
    eps = get_show_detail(client)["seasons"][0]["episodes"]
    client.post("/api/progress",
                json={"type": "episode", "id": eps[0]["id"],
                      "position": 40, "duration": 1800})
    pb = client.lib.get_playback("episode", eps[0]["id"])
    assert pb["position_sec"] == 40 and pb["watched"] == 0


def test_progress_90pct_marks_watched(client):
    movie_id = get_movie_id(client)
    client.post("/api/progress",
                json={"type": "movie", "id": movie_id, "position": 95, "duration": 100})
    assert client.lib.get_playback("movie", movie_id)["watched"] == 1


def test_progress_near_end_marks_watched_for_long_video(client):
    movie_id = get_movie_id(client)
    # 1 小时的片，剩 60 秒 → 已看
    client.post("/api/progress",
                json={"type": "movie", "id": movie_id,
                      "position": 3540, "duration": 3600})
    assert client.lib.get_playback("movie", movie_id)["watched"] == 1


def test_progress_short_video_not_marked_watched(client):
    movie_id = get_movie_id(client)
    # 5 分钟的短片剩 100 秒：「剩余 <3 分钟」只对 10 分钟以上的片子用，90% 也没到
    client.post("/api/progress",
                json={"type": "movie", "id": movie_id, "position": 200, "duration": 300})
    assert client.lib.get_playback("movie", movie_id)["watched"] == 0


def test_progress_null_duration_kept(client):
    movie_id = get_movie_id(client)
    client.post("/api/progress",
                json={"type": "movie", "id": movie_id,
                      "position": 60, "duration": None})
    pb = client.lib.get_playback("movie", movie_id)
    assert pb["duration_sec"] is None and pb["watched"] == 0


def test_progress_errors(client):
    assert client.post("/api/progress",
                       json={"type": "movie", "id": 9999,
                             "position": 1, "duration": None}).status_code == 404
    assert client.post("/api/progress",
                       json={"type": "person", "id": 1,
                             "position": 1, "duration": None}).status_code == 422


def test_watched_flag_shared_rules():
    """判定函数单点（IINA 与内嵌两条路共用），规则与 v1 一致。"""
    assert watched_flag(95, 100) is True     # 90%
    assert watched_flag(3540, 3600) is True  # 长片剩 <3 分钟
    assert watched_flag(200, 300) is False   # 短片剩 100 秒不判已看
    assert watched_flag(100, 1000) is False  # 远未到
    assert watched_flag(50, None) is False   # 无时长
    assert watched_flag(0, 1000) is False    # 没播过


# ---------- /api/next ----------

def test_next_episode_same_season(client):
    eps = get_show_detail(client)["seasons"][0]["episodes"]
    resp = client.get("/api/next", params={"type": "episode", "id": eps[0]["id"]})
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == eps[1]["id"]
    assert (data["season_number"], data["episode_number"]) == (1, 2)


def test_next_episode_last_returns_null(client):
    eps = get_show_detail(client)["seasons"][0]["episodes"]
    resp = client.get("/api/next", params={"type": "episode", "id": eps[1]["id"]})
    assert resp.status_code == 200
    assert resp.json() is None


def test_next_episode_crosses_season(tmp_path, cfg, lib):
    build_tree(tmp_path, seasons=((1, 2), (2, 1)))
    with make_client(tmp_path, cfg, lib) as client:
        detail = get_show_detail(client)
        by_key = {(e["season_number"], e["episode_number"]): e["id"]
                  for s in detail["seasons"] for e in s["episodes"]}
        # S01E02（季末）→ S02E01
        resp = client.get("/api/next", params={"type": "episode", "id": by_key[(1, 2)]})
        assert (resp.json()["season_number"], resp.json()["episode_number"]) == (2, 1)
        # S02E01（全剧最后一集）→ null
        resp = client.get("/api/next", params={"type": "episode", "id": by_key[(2, 1)]})
        assert resp.json() is None


def test_next_episode_errors(client):
    assert client.get("/api/next", params={"type": "show", "id": 1}).status_code == 422
    assert client.get("/api/next", params={"type": "episode", "id": 9999}).status_code == 404


# ---------- /api/scan/status 的 playing 只算 IINA 监控线程 ----------

def test_scan_status_playing_counts_only_iina_threads(client):
    movie_id = get_movie_id(client)
    client.post("/api/play",
                json={"type": "movie", "id": movie_id, "embedded": True})
    assert client.get("/api/scan/status").json()["playing"] == 0
    # IINA 路径起了监控线程（等 IPC socket 中）→ playing 为 1
    client.post("/api/play", json={"type": "movie", "id": movie_id})
    assert client.get("/api/scan/status").json()["playing"] == 1
