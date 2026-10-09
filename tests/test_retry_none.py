"""--retry-none：只重查库里记为 none 的集（此前误记成 none 的能转回 ok）。一律虚构片名。"""

import pytest

from homecinema.__main__ import cmd_community_segments
from homecinema.community import CommunityClient, run_community


class Resp:
    def __init__(self, code, data=None):
        self.status_code = code
        self._data = data

    def json(self):
        return self._data


class FakeHttp:
    def __init__(self, routes):
        self.routes = routes          # [(url 子串, 参数子集, Resp)]
        self.calls = []

    def get(self, url, params=None, headers=None):
        params = dict(params or {})
        self.calls.append((url, params))
        for sub, want, resp in self.routes:
            if sub in url and all(params.get(k) == v for k, v in want.items()):
                return resp
        return Resp(404, {"error": "media not found"})


def client(routes):
    return CommunityClient(http=FakeHttp(routes), sleep=lambda s: None, min_interval=0)


@pytest.fixture(autouse=True)
def no_probe(monkeypatch):
    monkeypatch.setattr("homecinema.community._duration", lambda ep, d: 1500.0)


def make_none_episodes(lib, title, n, tmdb_id, oldest=None):
    """建一剧 n 集，全部先落库成 status='none'；oldest 给定时按它从旧到新排 fetched_at。"""
    sid = lib.upsert_show(path=f"/tv/{title}", folder=f"/tv/{title}", title=title, year=None)
    lib.update_show_metadata(sid, {"tmdb_id": tmdb_id, "status": "matched"})
    ids = [lib.upsert_episode(show_id=sid, season_number=1, episode_number=e,
                              path=f"/tv/{title}/e{e}.mkv", video_name=f"e{e}", ext=".mkv",
                              title=f"e{e}", size=1, mtime=0) for e in range(1, n + 1)]
    for i, eid in enumerate(ids):
        lib.save_community_segment(eid, "theintrodb", "none", {})
        if oldest:
            lib._conn.execute("UPDATE community_segments SET fetched_at=? WHERE episode_id=?",
                              (f"{oldest}-{i:06d}", eid))
    lib._conn.commit()
    return ids


def test_only_none_are_requeried_and_ok_untouched(lib):
    ids = make_none_episodes(lib, "None Show", 2, 501)
    ok_id = make_none_episodes(lib, "Other Show", 1, 502)[0]
    lib.save_community_segment(ok_id, "theintrodb", "ok", {"intro_start": 1.0, "intro_end": 40.0})
    before = dict(lib.get_community_segment(ok_id))

    c = client([])
    stats = run_community(lib, c, tmdb=None, progress=lambda *a, **k: None, retry_none=True)
    assert stats["total"] == 2 and stats["none"] == 2       # ok 的那集不进本轮
    assert [params["episode"] for _, params in c._http.calls] == [1, 2]
    assert [lib.get_community_segment(e)["status"] for e in ids] == ["none", "none"]
    assert dict(lib.get_community_segment(ok_id)) == before  # ok 的记录完全没被碰


def test_none_converts_to_ok_and_writes_segments(lib):
    ids = make_none_episodes(lib, "Back Show", 2, 503)
    c = client([("theintrodb", {"episode": 1}, Resp(200, {
        "intro": [{"start_ms": 1000, "end_ms": 45000}],
        "credits": [{"start_ms": 2400000, "end_ms": None}]}))])
    stats = run_community(lib, c, tmdb=None, progress=lambda *a, **k: None, retry_none=True)
    assert stats["found"] == 1 and stats["none"] == 1
    r1 = lib.get_community_segment(ids[0])
    assert r1["status"] == "ok" and r1["provider"] == "theintrodb"
    assert (r1["intro_start"], r1["intro_end"], r1["credits_start"]) == (1.0, 45.0, 2400.0)
    assert lib.get_community_segment(ids[1])["status"] == "none"


def test_error_leaves_original_record_untouched(lib):
    ids = make_none_episodes(lib, "Err Show", 1, 504, oldest="2026-09-01T00:00:00")
    before = dict(lib.get_community_segment(ids[0]))
    c = client([("theintrodb", {}, Resp(503))])       # 重试耗尽 → RuntimeError
    stats = run_community(lib, c, tmdb=None, progress=lambda *a, **k: None, retry_none=True)
    assert stats["error"] == 1 and stats["found"] == 0 and stats["none"] == 0
    assert dict(lib.get_community_segment(ids[0])) == before    # 状态、provider、fetched_at 都没变


def test_still_none_refreshes_time_only(lib):
    ids = make_none_episodes(lib, "Keep Show", 1, 505, oldest="2026-09-01T00:00:00")
    stats = run_community(lib, client([]), tmdb=None, progress=lambda *a, **k: None, retry_none=True)
    row = lib.get_community_segment(ids[0])
    assert stats["none"] == 1
    assert row["status"] == "none"
    assert row["fetched_at"] > "2026-09-01T00:00:00-000000"   # 时间刷新，状态仍是 none


def test_quota_exceeded_stops_the_round(lib):
    ids = make_none_episodes(lib, "Quota Show", 4, 506, oldest="2026-09-02T00:00:00")
    quota = Resp(429, {"code": "usage_limit_exceeded", "retry_after": "18 hours"})
    c = client([("theintrodb", {}, quota)])
    msgs = []
    stats = run_community(lib, c, tmdb=None, progress=lambda msg, **k: msgs.append(msg),
                          retry_none=True)
    assert sum("theintrodb" in url for url, _ in c._http.calls) == 1    # 立刻停止，不再耗额度
    assert stats["error"] == 1 and stats["found"] == 0
    assert any("额度用完" in m and "已处理 1/4 集" in m for m in msgs)
    assert [lib.get_community_segment(e)["status"] for e in ids] == ["none"] * 4
    assert [lib.get_community_segment(e)["fetched_at"] for e in ids] == [
        f"2026-09-02T00:00:00-{i:06d}" for i in range(4)]        # 停止后原记录一律没被改写


def test_oldest_first_and_limit(lib):
    ids = make_none_episodes(lib, "Order Show", 3, 507, oldest="2026-09-03T00:00:00")
    # 故意让时间顺序和集号顺序相反：第 3 集最旧、第 1 集最新
    for eid, rank in zip([ids[2], ids[1], ids[0]], range(3)):
        lib._conn.execute("UPDATE community_segments SET fetched_at=? WHERE episode_id=?",
                          (f"2026-09-03T00:00:00-{rank:06d}", eid))
    lib._conn.commit()
    assert [r["id"] for r in lib.episodes_for_community(only_status="none")] == [ids[2], ids[1], ids[0]]
    c = client([])
    stats = run_community(lib, c, tmdb=None, progress=lambda *a, **k: None,
                          retry_none=True, limit=2)
    assert stats["total"] == 2 and stats["none"] == 2
    assert [params["episode"] for _, params in c._http.calls] == [3, 2]     # 只查最旧的两集
    assert lib.get_community_segment(ids[0])["fetched_at"] == "2026-09-03T00:00:00-000002"


def test_limit_without_retry_none_is_rejected(cfg, capsys):
    class Args:
        refresh = False
        retry_none = False
        limit = 10

    assert cmd_community_segments(cfg, Args()) == 2
    assert "--limit" in capsys.readouterr().err


def test_refresh_and_retry_none_are_exclusive(capsys):
    from homecinema.__main__ import main
    with pytest.raises(SystemExit) as exc:
        main(["community-segments", "--refresh", "--retry-none"])
    assert exc.value.code == 2
    assert "--retry-none" in capsys.readouterr().err
