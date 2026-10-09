"""审计测试：homecinema/metadata.py 的已确认 bug 复现。

每个 test_FNN_* 对应 AUDIT.md 里的一条发现，断言的是正确行为，
因此在当前代码上应当失败（这正是复现）。
"""

from __future__ import annotations

from homecinema.metadata import MetadataService, TmdbClient
from homecinema.scanner import run_scan


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, content=b""):
        self.status_code = status_code
        self._json = json_data
        self.content = content

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


class FakeHttpClient:
    """按 url 包含子串路由；responder 可为响应、响应列表（依次消费）或函数。"""

    def __init__(self):
        self.calls = []
        self.routes = []

    def add(self, predicate, responder):
        self.routes.append((predicate, responder))

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": dict(params or {})})
        for predicate, responder in self.routes:
            if predicate(url, dict(params or {})):
                if callable(responder):
                    return responder()
                if isinstance(responder, list):
                    return responder.pop(0) if len(responder) > 1 else responder[0]
                return responder
        raise AssertionError(f"unexpected GET {url} params={params}")


def url_has(substr):
    return lambda url, params: substr in url


TV_DETAIL_555 = {
    "id": 555, "name": "Star Vagabond", "overview": "简介", "genres": [],
    "vote_average": 8.0, "episode_run_time": [45],
    "poster_path": "/tvposter.jpg", "backdrop_path": None, "seasons": [],
}
MOVIE_DETAIL_101 = {
    "id": 101, "title": "Probe Movie", "overview": "简介", "genres": [],
    "vote_average": 7.0, "runtime": 100,
    "poster_path": "/mposter.jpg", "backdrop_path": None,
}


def seed_show(tmp_path, cfg, lib, folder_name="Star Vagabond (2003)"):
    show = tmp_path / "tv" / folder_name
    (show / "Season 01").mkdir(parents=True)
    (show / "Season 01" / "Star Vagabond S01E01.mkv").write_bytes(b"")
    run_scan(cfg, lib)
    return show


def seed_movie(tmp_path, cfg, lib, folder_name="Probe Movie (2001)"):
    folder = tmp_path / "movies" / folder_name
    folder.mkdir(parents=True)
    (folder / (folder_name + ".mp4")).write_bytes(b"")
    run_scan(cfg, lib)
    return folder


def make_service(cfg, lib, http, refresh=False, overrides=None):
    client = TmdbClient("test-key", cfg.tmdb_language, http=http,
                        sleep=lambda s: None, clock=lambda: 0.0,
                        min_interval=0.25)
    return MetadataService(lib, client, cfg.images_dir, refresh=refresh,
                           overrides=overrides)


def test_F01_tv_find_imdb_missing_year_guard(tmp_path, cfg, lib):
    """F01：enrich_show 对 find_by_imdb 的结果不做年份校验，nfo 里的旧版
    IMDb 号会直接配上年代差很远的同名剧（电影路径有 _year_close 守卫）。"""
    show = seed_show(tmp_path, cfg, lib)
    (show / "tvshow.nfo").write_text(
        '<tvshow><uniqueid type="imdb">tt0417299</uniqueid></tvshow>',
        encoding="utf-8")
    http = FakeHttpClient()
    # nfo 里的 IMDb 号其实指向 1978 年的原版（发布组 nfo 的典型错误）
    http.add(url_has("/find/"), FakeResponse(
        200, {"tv_results": [{"id": 900, "name": "Star Vagabond",
                              "first_air_date": "1978-09-17"}]}))
    http.add(url_has("/search/tv"), FakeResponse(
        200, {"results": [{"id": 555, "name": "Star Vagabond",
                           "first_air_date": "2003-10-27"}]}))
    http.add(url_has("/tv/900"), FakeResponse(200, TV_DETAIL_555 | {"id": 900}))
    http.add(url_has("/tv/555"), FakeResponse(200, TV_DETAIL_555 | {"id": 555}))
    http.add(url_has("image.tmdb.org"), FakeResponse(200, content=b"x"))
    service = make_service(cfg, lib, http)
    service.enrich_all()

    row = lib.get_show(lib.list_shows()[0]["id"])
    # 正确行为：1978 与文件夹年份 2003 差太远，不该信这个 IMDb 号，
    # 应回落到搜索并配上 2003 年的那部（与电影路径 _year_close 同一原则）
    assert row["tmdb_id"] == 555, f"配到了错误年份的剧: {row['tmdb_id']}"
    assert row["status"] == "matched"


def test_F02_refresh_rematch_stale_poster_cached(tmp_path, cfg, lib):
    """F02：refresh 重匹配成功但新详情没有海报/背景图时，旧匹配留下的
    poster_cached 不被清掉，库里继续挂着另一部片的海报。"""
    seed_movie(tmp_path, cfg, lib)
    folder_name = "Probe Movie (2001)"
    http = FakeHttpClient()
    http.add(url_has("/movie/101"), FakeResponse(200, MOVIE_DETAIL_101))
    http.add(url_has("image.tmdb.org"), FakeResponse(200, content=b"p101"))
    service = make_service(cfg, lib, http,
                           overrides={"movie": {folder_name: 101}})
    service.enrich_all()
    mid = lib.list_movies()[0]["id"]
    first = lib.get_movie(mid)
    assert first["status"] == "matched" and first["poster_cached"]

    http2 = FakeHttpClient()
    http2.add(url_has("/movie/102"), FakeResponse(
        200, MOVIE_DETAIL_101 | {"id": 102, "poster_path": None}))
    service2 = make_service(cfg, lib, http2, refresh=True,
                            overrides={"movie": {folder_name: 102}})
    service2.enrich_all()

    row = lib.get_movie(mid)
    assert row["tmdb_id"] == 102
    # 正确行为：新片 102 没有海报，不应继续挂着旧片 101 的海报缓存
    # （unmatched 分支对此有同样的清理逻辑，见 metadata.py 注释）
    assert row["poster_cached"] is None, f"残留旧海报: {row['poster_cached']}"
    assert row["poster_path"] is None
