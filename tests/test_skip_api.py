"""跳过片头片尾的 API：/api/play（embedded）的 skip 字段与 /api/segments。虚构片名。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from homecinema.player import Player
from homecinema.scanner import run_scan
from homecinema.server import ScanService, create_app


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
    app = create_app(config=cfg, db=lib, player=player, scan_service=ScanService(cfg, lib))
    with TestClient(app, base_url="http://127.0.0.1") as tc:
        tc.lib = lib  # type: ignore[attr-defined]
        yield tc


def episode(client, index=0) -> dict:
    show = client.get("/api/shows").json()[0]
    detail = client.get(f"/api/show/{show['id']}").json()
    return detail["seasons"][0]["episodes"][index]


def play(client, item_type, item_id) -> dict:
    resp = client.post("/api/play",
                       json={"type": item_type, "id": item_id, "embedded": True})
    assert resp.status_code == 200
    return resp.json()


def set_runtime(client, episode_id: int, minutes: int | None) -> None:
    client.lib._conn.execute("UPDATE episodes SET runtime_minutes=? WHERE id=?",
                             (minutes, episode_id))
    client.lib._conn.commit()


def test_movie_skip_is_null(client):
    movie = client.get("/api/movies").json()[0]
    assert play(client, "movie", movie["id"])["skip"] is None


def test_episode_without_segments_has_empty_skip(client):
    ep = episode(client)
    assert play(client, "episode", ep["id"])["skip"] == {"intro": None, "credits_start": None}


def test_episode_skip_intro_and_credits(client):
    ep = episode(client)
    set_runtime(client, ep["id"], 25)  # 25 分钟 = 1500 秒，后 40% 起点阈值 900
    client.lib.save_segments(ep["id"], (12.0, 72.0), (1400.0, 1480.0), "fingerprint")
    skip = play(client, "episode", ep["id"])["skip"]
    assert skip["intro"] == [12.0, 72.0]
    assert skip["credits_start"] == 1400.0


def test_credits_start_too_early_dropped(client):
    ep = episode(client)
    set_runtime(client, ep["id"], 25)
    # 起点 800 < 900（后 40% 之外）→ 不给
    client.lib.save_segments(ep["id"], None, (800.0, 900.0), "fingerprint")
    assert play(client, "episode", ep["id"])["skip"]["credits_start"] is None


def test_credits_start_falls_back_to_credits_end(client):
    ep = episode(client)
    set_runtime(client, ep["id"], None)  # 没有 runtime，用片尾段 end=1480 近似时长
    client.lib.save_segments(ep["id"], None, (1400.0, 1480.0), "fingerprint")
    skip = play(client, "episode", ep["id"])["skip"]
    assert skip["credits_start"] == 1400.0  # 1400 >= 0.6*1480


def test_credits_start_fallback_can_reject(client):
    ep = episode(client)
    set_runtime(client, ep["id"], None)
    client.lib.save_segments(ep["id"], None, (300.0, 1000.0), "fingerprint")
    assert play(client, "episode", ep["id"])["skip"]["credits_start"] is None


def test_credits_start_missing_start_yields_null(client):
    ep = episode(client)
    set_runtime(client, ep["id"], None)
    client.lib._conn.execute(
        "INSERT INTO segments (episode_id, credits_end, source, detected_at) "
        "VALUES (?, ?, 'fingerprint', '2026-10-02T00:00:00+00:00')", (ep["id"], 1480.0))
    client.lib._conn.commit()
    assert play(client, "episode", ep["id"])["skip"]["credits_start"] is None


def test_api_segments_endpoint(client):
    ep = episode(client)
    assert client.get("/api/segments", params={"episode_id": ep["id"]}).status_code == 404
    client.lib.save_segments(ep["id"], (12.0, 72.0), None, "chapters")
    resp = client.get("/api/segments", params={"episode_id": ep["id"]})
    assert resp.status_code == 200
    data = resp.json()
    assert data["episode_id"] == ep["id"]
    assert data["intro_start"] == 12.0 and data["intro_end"] == 72.0
    assert data["source"] == "chapters"
