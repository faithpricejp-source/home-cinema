"""社区片头片尾库：假 HTTP 客户端，虚构剧名。"""

from homecinema.community import CommunityClient, run_community


class Resp:
    def __init__(self, code, data=None):
        self.status_code = code
        self._data = data

    def json(self):
        return self._data


class FakeHttp:
    def __init__(self, routes):
        self.routes = routes  # [(url 子串, 参数子集, Resp)]
        self.calls = []

    def get(self, url, params=None, headers=None):
        self.calls.append((url, dict(params or {})))
        self.headers = headers
        for sub, want, resp in self.routes:
            if sub in url and all(params.get(k) == v for k, v in want.items()):
                return resp
        return Resp(404, {"error": "media not found"})


def client(routes):
    return CommunityClient(http=FakeHttp(routes), sleep=lambda s: None, min_interval=0)


def test_theintrodb_parses_null_intro_start_and_open_credits():
    c = client([("theintrodb", {"season": 1}, Resp(200, {
        "intro": [{"start_ms": None, "end_ms": 45000}],
        "credits": [{"start_ms": 2500000, "end_ms": None}],
        "preview": [{"start_ms": 2550000, "end_ms": None}]}))])
    seg = c.theintrodb(100, 1, 2, 2600.0)
    assert seg == {"intro_start": 0.0, "intro_end": 45.0, "credits_start": 2500.0,
                   "credits_end": None, "preview_start": 2550.0}
    assert c._http.calls[0][1]["duration_ms"] == 2600000


def test_introdb_maps_outro_to_credits():
    c = client([("api.introdb", {}, Resp(200, {
        "intro": {"start_ms": 2000, "end_ms": 58000}, "recap": None,
        "outro": {"start_ms": 3166000, "end_ms": 3217000}}))])
    assert c.introdb("tt0000001", 5, 15) == {"intro_start": 2.0, "intro_end": 58.0,
                                             "credits_start": 3166.0, "credits_end": 3217.0}


def test_not_found_is_none_and_server_error_raises():
    import pytest
    assert client([]).theintrodb(1, 1, 1, None) is None
    bad = client([("theintrodb", {}, Resp(503))])
    with pytest.raises(RuntimeError):
        bad.theintrodb(1, 1, 1, None)


class FakeTmdb:
    def external_ids(self, kind, tmdb_id):
        return {"imdb_id": "tt0000009"}


def test_run_community_falls_back_to_introdb_and_skips_done(lib, monkeypatch):
    sid = lib.upsert_show(path="/tv/Fake Show", folder="/tv/Fake Show", title="Fake Show", year=None)
    lib.update_show_metadata(sid, {"tmdb_id": 555, "status": "matched"})
    e1 = lib.upsert_episode(show_id=sid, season_number=1, episode_number=1, path="/tv/Fake Show/a.mkv",
                            video_name="a", ext=".mkv", title="a", size=1, mtime=0)
    e2 = lib.upsert_episode(show_id=sid, season_number=1, episode_number=2, path="/tv/Fake Show/b.mkv",
                            video_name="b", ext=".mkv", title="b", size=1, mtime=0)
    c = client([("theintrodb", {"episode": 1}, Resp(200, {"intro": [{"start_ms": 1000, "end_ms": 40000}]})),
                ("api.introdb", {"episode": 2}, Resp(200, {"intro": {"start_ms": 0, "end_ms": 30000}}))])
    monkeypatch.setattr("homecinema.community._duration", lambda ep, d: 1500.0)
    stats = run_community(lib, c, tmdb=FakeTmdb(), progress=lambda *a, **k: None)
    assert stats == {"total": 2, "found": 2, "none": 0, "error": 0}
    assert lib.get_community_segment(e1)["provider"] == "theintrodb"
    r2 = lib.get_community_segment(e2)
    assert r2["provider"] == "introdb" and r2["intro_end"] == 30.0
    assert lib.get_show(sid)["imdb_id"] == "tt0000009"
    assert run_community(lib, c, tmdb=FakeTmdb(), progress=lambda *a, **k: None)["total"] == 0


def test_quota_exceeded_stops_theintrodb_and_leaves_unchecked(lib, monkeypatch):
    sid = lib.upsert_show(path="/tv/Q Show", folder="/tv/Q Show", title="Q Show", year=None)
    lib.update_show_metadata(sid, {"tmdb_id": 777, "status": "matched"})
    e1 = lib.upsert_episode(show_id=sid, season_number=1, episode_number=1, path="/tv/Q Show/a.mkv",
                            video_name="a", ext=".mkv", title="a", size=1, mtime=0)
    e2 = lib.upsert_episode(show_id=sid, season_number=1, episode_number=2, path="/tv/Q Show/b.mkv",
                            video_name="b", ext=".mkv", title="b", size=1, mtime=0)
    quota = Resp(429, {"code": "usage_limit_exceeded", "retry_after": "18 hours"})
    c = client([("theintrodb", {}, quota),
                ("api.introdb", {"episode": 2}, Resp(200, {"intro": {"start_ms": 0, "end_ms": 30000}}))])
    monkeypatch.setattr("homecinema.community._duration", lambda ep, d: 1500.0)
    stats = run_community(lib, c, tmdb=FakeTmdb(), progress=lambda *a, **k: None)
    assert stats["found"] == 1 and stats["error"] == 1
    assert lib.get_community_segment(e1) is None          # 没查全，不落库
    assert lib.get_community_segment(e2)["provider"] == "introdb"
    assert sum("theintrodb" in url for url, _ in c._http.calls) == 1  # 额度用完后不再请求


def test_theintrodb_key_sent_as_bearer():
    http = FakeHttp([("theintrodb", {}, Resp(200, {"intro": [{"start_ms": 0, "end_ms": 30000}]}))])
    c = CommunityClient(http=http, sleep=lambda s: None, min_interval=0, theintrodb_key="k123")
    assert c.theintrodb(1, 1, 1, None)["intro_end"] == 30.0
    assert http.headers == {"Authorization": "Bearer k123"}
