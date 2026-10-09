"""Claude 验收时写的回归测试：钉住真库实测中发现并修掉的问题。一律用虚构片名。"""

from __future__ import annotations

import json
import os
import socket
import threading

from conftest import wait_until

from homecinema.metadata import MetadataService, TmdbClient
from homecinema.player import Player
from homecinema.scanner import EPISODE_TAG, run_scan


# ---------- 播放：片子加载期间属性拿不到，不能当成播放结束 ----------

class LoadingMpv:
    """前几轮 time-pos/duration 都回 property unavailable（片子在加载），之后正常，最后断开。"""

    def __init__(self, path, script):
        self.script = list(script)  # 每项: None=unavailable，数字=time-pos
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(str(path))
        self.srv.listen(1)
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        conn, _ = self.srv.accept()
        reader = conn.makefile("rb")
        current = None
        while True:
            line = reader.readline()
            if not line:
                break
            name = json.loads(line)["command"][1]
            if name == "time-pos":
                if not self.script:
                    break
                current = self.script.pop(0)
            if current is None:
                msg = {"request_id": 0, "error": "property unavailable"}
            else:
                msg = {"request_id": 0, "error": "success",
                       "data": current if name == "time-pos" else 3000.0}
            conn.sendall(json.dumps(msg).encode() + b"\n")
        conn.close()
        self.srv.close()


def _movie(tmp_path, cfg, lib):
    d = tmp_path / "movies" / "Example Movie (2001)"
    d.mkdir(parents=True)
    (d / "Example Movie (2001).mkv").write_bytes(b"x")
    run_scan(cfg, lib)
    return lib.list_movies()[0]["id"]


def test_progress_saved_after_slow_load(tmp_path, cfg, lib, sock_dir):
    movie_id = _movie(tmp_path, cfg, lib)
    sock = sock_dir / "p.sock"
    LoadingMpv(sock, [None, None, None, 120.0, 900.0])
    player = Player(lib, cfg, launcher=lambda *a, **k: None, poll_interval=0.01)
    player.play("movie", movie_id, sock_path=sock)
    player.wait_all(5)
    pb = lib.get_playback("movie", movie_id)
    assert pb is not None and pb["position_sec"] == 900.0


def test_gives_up_when_never_loads(tmp_path, cfg, lib, sock_dir):
    movie_id = _movie(tmp_path, cfg, lib)
    sock = sock_dir / "q.sock"
    LoadingMpv(sock, [None] * 1000)
    player = Player(lib, cfg, launcher=lambda *a, **k: None, poll_interval=0.01,
                    startup_grace=0.2)
    player.play("movie", movie_id, sock_path=sock)
    player.wait_all(5)
    assert player.active_count() == 0
    assert lib.get_playback("movie", movie_id) is None


def test_short_clip_not_marked_watched_at_start(tmp_path, cfg, lib):
    movie_id = _movie(tmp_path, cfg, lib)
    player = Player(lib, cfg, launcher=lambda *a, **k: None)
    player._save("movie", movie_id, 5.0, 120.0)  # 2 分钟短片，刚放 5 秒
    assert not lib.get_playback("movie", movie_id)["watched"]
    player._save("movie", movie_id, 2900.0, 3000.0)  # 长片剩不到 3 分钟
    assert lib.get_playback("movie", movie_id)["watched"]


# ---------- 扫描 ----------

def test_double_episode_filename():
    assert EPISODE_TAG.search("Sample Show S06E01E02.mkv").groups() == ("06", "01")
    assert EPISODE_TAG.search("Sample Show S01E10.mp4").groups() == ("01", "10")
    assert EPISODE_TAG.search("Sample Show S01E101.mp4").groups() == ("01", "101")


def test_added_at_uses_file_time_not_scan_time(tmp_path, cfg, lib):
    d = tmp_path / "movies" / "Old Movie (1990)"
    d.mkdir(parents=True)
    f = d / "Old Movie (1990).mkv"
    f.write_bytes(b"x")
    os.utime(f, (946684800, 946684800))  # 2000-01-01
    run_scan(cfg, lib)
    assert lib.list_movies()[0]["added_at"].startswith("2000-01-01")


def test_recently_added_one_card_per_show(tmp_path, cfg, lib):
    for e in range(1, 6):
        p = tmp_path / "tv" / "Sample Show" / "Season 01"
        p.mkdir(parents=True, exist_ok=True)
        (p / f"Sample Show S01E{e:02d}.mkv").write_bytes(b"x")
    run_scan(cfg, lib)
    assert [r["type"] for r in lib.recently_added(20)] == ["show"]


# ---------- 元数据 ----------

class Resp:
    def __init__(self, status, data=None, content=b""):
        self.status_code, self._data, self.content = status, data, content

    def json(self):
        return self._data


class TimeoutThenOk:
    """第一次请求抛网络异常，之后正常。"""

    def __init__(self):
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        if len(self.calls) == 1:
            raise TimeoutError("read timed out")
        if "/search/movie" in url:
            if {"year", "primary_release_year"} & set(params or {}):
                return Resp(200, {"results": []})  # 带年份搜不到
            return Resp(200, {"results": [{"id": 5, "title": "Example Movie",
                                           "release_date": "2002-05-01"}]})
        if "/movie/5" in url:
            return Resp(200, {"id": 5, "overview": "简介", "genres": [],
                              "poster_path": "/p.jpg", "backdrop_path": "/b.jpg"})
        if "image.tmdb.org" in url:
            return Resp(200, content=b"img")
        return Resp(404)


def test_network_error_retried_and_year_fallback(tmp_path, cfg, lib):
    _movie(tmp_path, cfg, lib)
    http = TimeoutThenOk()
    client = TmdbClient("k", http=http, sleep=lambda s: None)
    stats = MetadataService(lib, client, cfg.images_dir).enrich_all()
    assert stats["matched"] == 1 and stats["failed"] == 0
    m = lib.list_movies()[0]
    assert m["status"] == "matched" and m["tmdb_id"] == 5
    urls = [u for u, _ in http.calls]
    assert any("/t/p/w500/p.jpg" in u for u in urls)  # 海报不下原图
    assert any("/t/p/w1280/b.jpg" in u for u in urls)


class AlwaysBroken:
    def get(self, url, params=None, timeout=None):
        raise ConnectionError("network down")


def test_network_down_does_not_abort_run(tmp_path, cfg, lib):
    _movie(tmp_path, cfg, lib)
    client = TmdbClient("k", http=AlwaysBroken(), sleep=lambda s: None)
    stats = MetadataService(lib, client, cfg.images_dir).enrich_all()
    assert stats["total"] == 1  # 跑完了，没有抛出
    assert lib.list_movies()[0]["status"] == "unmatched"  # 下次还会重试


# ---------- 海报墙卡片要有跳转地址（ZCode 版本缺 href，点海报跳到 /undefined） ----------

def test_list_payloads_have_href(tmp_path, cfg, lib):
    from fastapi.testclient import TestClient
    from homecinema.server import create_app
    _movie(tmp_path, cfg, lib)
    p = tmp_path / "tv" / "Sample Show" / "Season 01"
    p.mkdir(parents=True)
    (p / "Sample Show S01E01.mkv").write_bytes(b"x")
    run_scan(cfg, lib)
    c = TestClient(create_app(config=cfg, db=lib), base_url="http://127.0.0.1")
    home = c.get("/api/home").json()
    for item in home["movies"] + home["shows"] + home["recently_added"] + c.get("/api/movies").json() + c.get("/api/shows").json():
        assert item["href"].startswith(("#/movie/", "#/show/"))


# ---------- 匹配：TMDB year 过滤很松，第一条常是别的年代的同名片 ----------

class SearchOnly:
    def __init__(self, results, find=None):
        self.results, self.find, self.calls = results, find, []

    def get(self, url, params=None, timeout=None):
        self.calls.append(url)
        if "/find/" in url:
            return Resp(200, {"movie_results": self.find or [], "tv_results": []})
        return Resp(200, {"results": self.results})


def test_wrong_year_first_result_rejected():
    http = SearchOnly([
        {"id": 1, "title": "True Example", "release_date": "1981-09-25"},
        {"id": 2, "title": "Example", "release_date": "2010-06-05"},
    ])
    c = TmdbClient("k", http=http, sleep=lambda s: None)
    assert c.search_movie("Example", 2010)["id"] == 2


def test_no_plausible_candidate_means_unmatched():
    http = SearchOnly([{"id": 1, "title": "Something Else", "release_date": "1981-01-01"}])
    c = TmdbClient("k", http=http, sleep=lambda s: None)
    assert c.search_movie("Example", 2010) is None


def test_title_punctuation_normalized():
    http = SearchOnly([{"id": 7, "title": "Saga: Part Two", "release_date": "2013-01-01"}])
    c = TmdbClient("k", http=http, sleep=lambda s: None)
    assert c.search_movie("Saga - Part Two", 2013)["id"] == 7


def test_imdb_id_in_release_nfo_used(tmp_path, cfg, lib):
    _movie(tmp_path, cfg, lib)
    folder = tmp_path / "movies" / "Example Movie (2001)"
    (folder / "Example Movie (2001).nfo").write_text("IMDB URL....: http://www.imdb.com/title/tt0000042/\n")
    http = SearchOnly([], find=[{"id": 42}])
    client = TmdbClient("k", http=http, sleep=lambda s: None)
    MetadataService(lib, client, cfg.images_dir).enrich_movie(lib.list_movies()[0])
    assert any("/find/tt0000042" in u for u in http.calls)
    assert any("/movie/42" in u for u in http.calls)


class LocalizedSearch:
    """模拟真实 TMDB：zh-CN 搜索返回的 title 是中文译名。"""

    def get(self, url, params=None, timeout=None):
        zh = (params or {}).get("language") == "zh-CN"
        return Resp(200, {"results": [
            {"id": 9, "title": "同年无关片" if zh else "Unrelated Film", "original_title": "x",
             "release_date": "2001-01-01", "vote_count": 999},
            {"id": 3, "title": "距离" if zh else "Distance", "original_title": "ディスタンス",
             "release_date": "2001-05-26", "vote_count": 10},
        ]})


def test_match_uses_english_titles_not_localized():
    c = TmdbClient("k", language="zh-CN", http=LocalizedSearch(), sleep=lambda s: None)
    assert c.search_movie("Distance", 2001)["id"] == 3


def test_nfo_actor_tmdbid_ignored(tmp_path):
    from homecinema.metadata import parse_nfo_tmdbid
    (tmp_path / "a.nfo").write_text(
        "<movie><title>X</title><actor><name>A</name><tmdbid>500</tmdbid></actor>"
        "<uniqueid type=\"tmdb\">744</uniqueid></movie>")
    assert parse_nfo_tmdbid(tmp_path) == 744
    (tmp_path / "a.nfo").write_text(
        "<movie><title>X</title><actor><name>A</name><tmdbid>500</tmdbid></actor></movie>")
    assert parse_nfo_tmdbid(tmp_path) is None


def test_sequel_numerals_and_containment():
    http = SearchOnly([{"id": 4, "title": "Aces Go Places III: Our Man from Bond Street",
                        "release_date": "1984-02-01"}])
    c = TmdbClient("k", http=http, sleep=lambda s: None)
    assert c.search_movie("Aces Go Places 3", 1984)["id"] == 4


def test_show_query_strips_parenthetical_and_prefix_match():
    http2 = SearchOnly([{"id": 9, "name": "Example Show: Subtitle", "first_air_date": "2014-01-01"}])
    c2 = TmdbClient("k", http=http2, sleep=lambda s: None)
    assert c2.search_tv("Example Show (US)")["id"] == 9


def test_override_file_wins(tmp_path, cfg, lib):
    _movie(tmp_path, cfg, lib)
    ov = tmp_path / "overrides.toml"
    ov.write_text('[movie]\n"Example Movie (2001)" = 77\n')
    cfg.overrides_file = str(ov)
    http = SearchOnly([])
    client = TmdbClient("k", http=http, sleep=lambda s: None)
    MetadataService(lib, client, cfg.images_dir, overrides=cfg.read_overrides()).enrich_movie(lib.list_movies()[0])
    assert any("/movie/77" in u for u in http.calls)
    assert not any("/search/" in u for u in http.calls)
