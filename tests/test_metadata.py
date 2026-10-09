"""元数据测试：假 HTTP 客户端，覆盖 nfo 优先、本地图优先、语言回退、429 重试、unmatched。"""

from __future__ import annotations

import hashlib

from homecinema.metadata import (MetadataService, TmdbClient, cache_local_image,
                                 find_local_poster, parse_nfo_tmdbid)
from homecinema.scanner import run_scan

MOVIE_JSON = {
    "id": 101, "overview": "中文简介", "title": "Example Movie",
    "genres": [{"id": 1, "name": "剧情"}, {"id": 2, "name": "科幻"}],
    "vote_average": 7.8, "runtime": 100,
    "poster_path": "/poster101.jpg", "backdrop_path": "/backdrop101.jpg",
}
MOVIE_EN_JSON = {"id": 101, "overview": "English overview", "title": "Example Movie"}
# 字段照真实 TMDB：电影结果带 release_date，剧集结果用 name 和 first_air_date
SEARCH_MOVIE_JSON = {"results": [{"id": 101, "title": "Example Movie", "release_date": "2001-03-01"}]}
TV_JSON = {
    "id": 202, "overview": "剧集简介", "title": "Sample Show",
    "genres": [{"id": 3, "name": "悬疑"}], "vote_average": 8.2,
    "episode_run_time": [45],
    "poster_path": "/showposter.jpg", "backdrop_path": "/showbackdrop.jpg",
    "seasons": [{"season_number": 1, "name": "第 1 季", "episode_count": 2,
                 "poster_path": None}],
}
SEASON1_JSON = {"name": "第 1 季", "episodes": [
    {"episode_number": 1, "id": 9001, "name": "第一集标题", "still_path": "/s1e1.jpg",
     "runtime": 45, "overview": "第一集简介", "air_date": "2001-01-01"},
    {"episode_number": 2, "id": 9002, "name": "第二集标题", "still_path": "/s1e2.jpg",
     "runtime": 45, "overview": "第二集简介", "air_date": "2001-01-08"},
]}
SEARCH_TV_JSON = {"results": [{"id": 202, "name": "Sample Show", "first_air_date": "2010-01-01"}]}


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
    """按 (url 包含子串, params 断言) 路由；responder 可为响应、响应列表（依次消费）或函数。"""

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


def make_client(**routes):
    client = FakeHttpClient()
    for predicate, responder in routes:
        client.add(predicate, responder)
    return client


def make_service(cfg, lib, http):
    sleeps = []
    client = TmdbClient("test-key", cfg.tmdb_language, http=http,
                        sleep=sleeps.append, clock=lambda: 0.0, min_interval=0.25)
    service = MetadataService(lib, client, cfg.images_dir)
    return service, sleeps, client


def seed_tree(tmp_path, cfg, lib):
    movies = tmp_path / "movies"
    folder = movies / "Example Movie (2001)"
    folder.mkdir(parents=True)
    (folder / "Example Movie (2001).mp4").write_bytes(b"")
    tv = tmp_path / "tv"
    show = tv / "Sample Show"
    (show / "Season 01").mkdir(parents=True)
    (show / "Season 01" / "Sample Show S01E01.mkv").write_bytes(b"")
    (show / "Season 01" / "Sample Show S01E02.mkv").write_bytes(b"")
    run_scan(cfg, lib)
    return folder, show


def test_nfo_tmdbid_priority_skips_search(tmp_path, cfg, lib):
    folder, _show = seed_tree(tmp_path, cfg, lib)
    (folder / "Example Movie (2001).nfo").write_text(
        '<movie><title>Example Movie</title><tmdbid>777</tmdbid></movie>',
        encoding="utf-8")
    http = make_client()
    http.add(lambda url, p: "/movie/777" in url, FakeResponse(200, MOVIE_JSON | {"id": 777}))
    http.add(lambda u, p: "/search/tv" in u, FakeResponse(200, SEARCH_TV_JSON))
    http.add(lambda u, p: "/tv/202" in u and "/season/" not in u, FakeResponse(200, TV_JSON))
    http.add(lambda u, p: "/season/1" in u, FakeResponse(200, SEASON1_JSON))
    http.add(url_has("image.tmdb.org"), FakeResponse(200, content=b"image-bytes"))
    service, _sleeps, client = make_service(cfg, lib, http)
    stats = service.enrich_all()
    assert stats["matched"] == 2  # 电影 + 剧集

    movie = lib.get_movie(lib.list_movies()[0]["id"])
    assert movie["tmdb_id"] == 777 and movie["status"] == "matched"
    assert movie["overview"] == "中文简介" and movie["runtime_minutes"] == 100
    assert not any("/search/movie" in c["url"] for c in http.calls)  # 没有搜索


def test_nfo_uniqueid_type_tmdb(tmp_path, cfg, lib):
    folder, _show = seed_tree(tmp_path, cfg, lib)
    (folder / "info.nfo").write_text(
        '<episodedetails><uniqueid type="tmdb" default="true">888</uniqueid>'
        "</episodedetails>", encoding="utf-8")
    assert parse_nfo_tmdbid(folder) == 888


def test_search_used_without_nfo(tmp_path, cfg, lib):
    seed_tree(tmp_path, cfg, lib)
    http = make_client()
    http.add(url_has("/search/movie"), FakeResponse(200, SEARCH_MOVIE_JSON))
    http.add(url_has("/search/tv"), FakeResponse(200, SEARCH_TV_JSON))
    http.add(url_has("/movie/101"), FakeResponse(200, MOVIE_JSON))
    http.add(lambda u, p2: "/tv/202" in u and "/season/" not in u, FakeResponse(200, TV_JSON))
    http.add(url_has("/season/1"), FakeResponse(200, SEASON1_JSON))
    http.add(url_has("image.tmdb.org"), FakeResponse(200, content=b"image-bytes"))
    service, _sleeps, client = make_service(cfg, lib, http)
    stats = service.enrich_all()
    assert stats["matched"] == 2

    movie = lib.get_movie(lib.list_movies()[0]["id"])
    assert movie["tmdb_id"] == 101 and movie["rating"] == 7.8
    import json
    assert json.loads(movie["genres"]) == ["剧情", "科幻"]
    assert movie["poster_cached"] == hashlib.sha256(b"w500/poster101.jpg").hexdigest()[:24] + ".jpg"
    assert (cfg.images_dir / movie["poster_cached"]).is_file()
    assert movie["backdrop_cached"] and (cfg.images_dir / movie["backdrop_cached"]).is_file()

    show = lib.get_show(lib.list_shows()[0]["id"])
    assert show["tmdb_id"] == 202 and show["runtime_minutes"] == 45
    eps = lib.episodes_for_show(show["id"])
    assert [e["title"] for e in eps] == ["第一集标题", "第二集标题"]
    assert all(e["still_cached"] and (cfg.images_dir / e["still_cached"]).is_file() for e in eps)
    seasons = lib.seasons_for_show(show["id"])
    assert seasons[0]["season_number"] == 1 and seasons[0]["name"] == "第 1 季"
    # 语言参数：默认 zh-CN
    zh_calls = [c for c in http.calls if c["params"].get("language") == "zh-CN"]
    assert zh_calls


def test_local_poster_wins_over_tmdb(tmp_path, cfg, lib):
    folder, _show = seed_tree(tmp_path, cfg, lib)
    local_poster = folder / "poster.jpg"
    local_poster.write_bytes(b"local-poster-bytes")

    http = make_client()
    http.add(url_has("/search/movie"), FakeResponse(200, SEARCH_MOVIE_JSON))
    http.add(url_has("/search/tv"), FakeResponse(200, SEARCH_TV_JSON))
    http.add(url_has("/movie/101"), FakeResponse(200, MOVIE_JSON))
    http.add(lambda u, p2: "/tv/202" in u and "/season/" not in u, FakeResponse(200, TV_JSON))
    http.add(url_has("/season/1"), FakeResponse(200, SEASON1_JSON))
    http.add(url_has("image.tmdb.org"), FakeResponse(200, content=b"tmdb-bytes"))
    service, _sleeps, client = make_service(cfg, lib, http)
    service.enrich_all()

    movie = lib.get_movie(lib.list_movies()[0]["id"])
    assert movie["poster_local"] == str(local_poster)
    assert movie["poster_cached"] and movie["poster_cached"].startswith("local_")
    cached = cfg.images_dir / movie["poster_cached"]
    assert cached.is_file() and cached.read_bytes() == b"local-poster-bytes"
    assert find_local_poster(folder, "Example Movie (2001).mp4") == local_poster
    # TMDB 海报没被下载（本地优先），背景图仍下载
    assert not any("poster101" in c["url"] for c in http.calls)
    assert any("backdrop101" in c["url"] for c in http.calls)


def test_zh_overview_empty_falls_back_to_en(tmp_path, cfg, lib):
    seed_tree(tmp_path, cfg, lib)
    zh_empty = dict(MOVIE_JSON, overview="")
    http = make_client()
    http.add(lambda u, p: "/search/movie" in u, FakeResponse(200, SEARCH_MOVIE_JSON))
    http.add(lambda u, p: "/search/tv" in u, FakeResponse(200, SEARCH_TV_JSON))
    http.add(lambda u, p: "/movie/101" in u and p.get("language") == "zh-CN",
             FakeResponse(200, zh_empty))
    http.add(lambda u, p: "/movie/101" in u and p.get("language") == "en-US",
             FakeResponse(200, MOVIE_EN_JSON))
    http.add(lambda u, p: "/tv/202" in u and "/season/" not in u, FakeResponse(200, TV_JSON))
    http.add(lambda u, p: "/season/1" in u, FakeResponse(200, SEASON1_JSON))
    http.add(url_has("image.tmdb.org"), FakeResponse(200, content=b"x"))
    service, _sleeps, client = make_service(cfg, lib, http)
    service.enrich_all()

    movie = lib.get_movie(lib.list_movies()[0]["id"])
    assert movie["overview"] == "English overview"
    en_calls = [c for c in http.calls if c["params"].get("language") == "en-US"]
    assert en_calls  # 确实发过 en-US 请求


def test_429_retries_with_backoff(tmp_path, cfg, lib):
    seed_tree(tmp_path, cfg, lib)
    state = {"n": 0}

    def flaky_search():
        state["n"] += 1
        if state["n"] == 1:
            return FakeResponse(429)
        return FakeResponse(200, SEARCH_MOVIE_JSON)

    http = make_client()
    http.add(lambda u, p: "/search/movie" in u, flaky_search)
    http.add(lambda u, p: "/search/tv" in u, FakeResponse(200, SEARCH_TV_JSON))
    http.add(lambda u, p: "/movie/101" in u, FakeResponse(200, MOVIE_JSON))
    http.add(lambda u, p: "/tv/202" in u and "/season/" not in u, FakeResponse(200, TV_JSON))
    http.add(lambda u, p: "/season/1" in u, FakeResponse(200, SEASON1_JSON))
    http.add(url_has("image.tmdb.org"), FakeResponse(200, content=b"x"))
    service, sleeps, client = make_service(cfg, lib, http)
    stats = service.enrich_all()

    assert stats["matched"] == 2
    search_calls = [c for c in http.calls if "/search/movie" in c["url"]]
    assert len(search_calls) >= 2  # 429 之后重试
    assert sleeps and all(s > 0 for s in sleeps)  # 退避 sleep 被调用


def test_unmatched_keeps_folder_title(tmp_path, cfg, lib):
    seed_tree(tmp_path, cfg, lib)
    http = make_client()
    http.add(lambda u, p: "/search/movie" in u, FakeResponse(200, {"results": []}))
    http.add(lambda u, p: "/search/tv" in u, FakeResponse(200, {"results": []}))
    service, _sleeps, _client = make_service(cfg, lib, http)
    stats = service.enrich_all()
    assert stats["unmatched"] == 2

    movie = lib.get_movie(lib.list_movies()[0]["id"])
    assert movie["status"] == "unmatched" and movie["title"] == "Example Movie"
    assert movie["tmdb_id"] is None
    show = lib.get_show(lib.list_shows()[0]["id"])
    assert show["status"] == "unmatched" and show["title"] == "Sample Show"


def test_image_cache_hash_and_reuse(tmp_path, cfg, lib):
    images = cfg.images_dir
    http = make_client()
    http.add(lambda u, p: "/fail.jpg" in u, FakeResponse(500))  # 失败不缓存
    http.add(url_has("image.tmdb.org"), FakeResponse(200, content=b"same-bytes"))
    sleeps = []
    client = TmdbClient("k", "zh-CN", http=http, sleep=sleeps.append,
                        clock=lambda: 0.0, min_interval=0.25)
    name1 = client.download_image("/posterX.jpg", images)
    name2 = client.download_image("/posterX.jpg", images)
    assert name1 == name2
    assert name1 == hashlib.sha256(b"w500/posterX.jpg").hexdigest()[:24] + ".jpg"
    assert (images / name1).read_bytes() == b"same-bytes"
    image_calls = [c for c in http.calls if "image.tmdb.org" in c["url"]]
    assert len(image_calls) == 1  # 已存在不重下

    assert client.download_image("/fail.jpg", images) is None  # 失败响应不落盘
    fail_hash = hashlib.sha256(b"w500/fail.jpg").hexdigest()[:24]
    assert not any(f.name.startswith(fail_hash) for f in images.iterdir())


def test_cache_local_image(tmp_path, cfg, lib):
    src = tmp_path / "poster.png"
    src.write_bytes(b"png-bytes")
    name = cache_local_image(src, cfg.images_dir)
    assert name.startswith("local_") and name.endswith(".png")
    assert (cfg.images_dir / name).read_bytes() == b"png-bytes"
    assert cache_local_image(src, cfg.images_dir) == name  # 重复拷贝返回同名
    assert cache_local_image(tmp_path / "missing.jpg", cfg.images_dir) is None


def test_chinese_title_search(tmp_path, cfg, lib):
    """英文文件夹名的片，用 TMDB 中文译名（简/繁、带标点）也能搜到；剧集同理。"""
    seed_tree(tmp_path, cfg, lib)
    http = make_client()
    trans = {"translations": {"translations": [
        {"iso_639_1": "zh", "iso_3166_1": "CN", "data": {"title": "你丫闭嘴！"}},
        {"iso_639_1": "zh", "iso_3166_1": "TW", "data": {"title": "終極剋星"}},
        {"iso_639_1": "ja", "iso_3166_1": "JP", "data": {"title": "ダマレ"}}]}}
    http.add(url_has("/search/movie"), FakeResponse(200, SEARCH_MOVIE_JSON))
    http.add(lambda u, p: "/movie/101" in u, FakeResponse(200, MOVIE_JSON | trans))
    http.add(lambda u, p: "/search/tv" in u, FakeResponse(200, SEARCH_TV_JSON))
    http.add(lambda u, p: "/tv/202" in u and "/season/" not in u,
             FakeResponse(200, {k: v for k, v in TV_JSON.items() if k != "title"} | {"name": "权力的游戏"}))
    http.add(lambda u, p: "/season/1" in u, FakeResponse(200, SEASON1_JSON))
    http.add(url_has("image.tmdb.org"), FakeResponse(200, content=b"image-bytes"))
    service, _sleeps, _client = make_service(cfg, lib, http)
    service.enrich_all()
    for q in ("你丫闭嘴", "終極剋星", "example movie", "EXAMPLE-movie"):
        assert len(lib.list_movies(q=q)) == 1, q
    assert lib.list_movies(q="ダマレ") == []  # 日文译名不收
    assert len(lib.list_shows(q="权力")) == 1


def test_search_by_people_and_character(tmp_path, cfg, lib):
    """10-07：搜演员/导演/主创名或角色名也能搜到片（电影走分组列表，剧集同理）。"""
    seed_tree(tmp_path, cfg, lib)
    movie = lib.list_movies()[0]
    show = lib.list_shows()[0]
    lib.save_credits("movie", movie["id"], [
        {"id": 1, "name": "洪金宝", "role": "director", "ord": 0},
        {"id": 2, "name": "元彪", "role": "cast", "character": "Leung Foon", "ord": 0}])
    lib.save_credits("show", show["id"], [
        {"id": 3, "name": "大卫·贝尼奥夫", "role": "creator", "ord": 0},
        {"id": 4, "name": "艾米莉亚·克拉克", "role": "cast", "character": "Daenerys Targaryen", "ord": 0}])
    for q in ("洪金宝", "元彪", "leung foon", "Leung-Foon"):
        assert [r["id"] for r in lib.list_movies(q=q)] == [movie["id"]], q
        assert [g["row"]["id"] for g in lib.list_movie_groups(q=q)] == [movie["id"]], q
    for q in ("贝尼奥夫", "克拉克", "daenerys"):
        assert [r["id"] for r in lib.list_shows(q=q)] == [show["id"]], q
    assert lib.list_movies(q="克拉克") == []  # 剧集的人不串到电影
    assert lib.list_shows(q="洪金宝") == []
    assert lib.list_movies(q="不存在的人") == []


def test_search_by_person_aka_and_aka_backfill(tmp_path, cfg, lib):
    """10-07：外国人搜英文名/原名，中国人搜别名也中；别名按人补拉、失败的下次再补。"""
    from homecinema.extras import person_aka, run_people_aka

    seed_tree(tmp_path, cfg, lib)
    movie = lib.list_movies()[0]
    lib.save_credits("movie", movie["id"], [
        {"id": 7, "name": "艾米莉亚·克拉克", "role": "cast", "ord": 0},
        {"id": 8, "name": "洪金宝", "role": "director", "ord": 1},
        {"id": 9, "name": "Doreen Lang", "role": "cast", "ord": 2}])

    class Client:
        def person_detail(self, pid):
            return {7: {"id": 7, "name": "Emilia Clarke", "also_known_as": ["Emilia Isobel Euphemia Rose Clarke"]},
                    8: {"id": 8, "name": "Sammo Hung", "also_known_as": [" 'Sammo Hung' ", "洪金寶", "홍금보"]},
                    9: None}[pid]

    assert lib.list_movies(q="Emilia Clarke") == []
    stats = run_people_aka(lib, Client(), progress=None)
    assert stats == {"total": 3, "ok": 2, "failed": 1}
    for q in ("emilia clarke", "Rose Clarke", "sammo hung", "洪金寶", "홍금보", "克拉克", "Doreen"):
        assert [r["id"] for r in lib.list_movies(q=q)] == [movie["id"]], q
    assert lib.people_needing_aka() == [9]  # 失败的人留到下次
    lib.save_credits("movie", movie["id"], [{"id": 7, "name": "艾米莉亚·克拉克", "role": "cast", "ord": 0}])
    assert [r["id"] for r in lib.list_movies(q="Euphemia")] == [movie["id"]]  # 重存演职员不冲掉别名
    assert person_aka({"id": 8, "name": "Sammo Hung", "also_known_as": ["Sammo Hung", '"大哥大"']}) == ["Sammo Hung", "大哥大"]
    assert person_aka(None) is None


def test_import_people_zh_from_wikidata_csv(tmp_path, cfg, lib):
    """10-07：外国影人的 Wikidata 中文名单独存 aka_zh，按中文译名能搜到；重拉 TMDB 别名不冲掉。"""
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "import_people_zh", Path(__file__).resolve().parents[1] / "tools" / "import_people_zh.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    seed_tree(tmp_path, cfg, lib)
    movie = lib.list_movies()[0]
    lib.save_credits("movie", movie["id"], [{"id": 29545, "name": "Claire Bloom", "role": "cast", "ord": 0}])
    src = tmp_path / "zh.csv"
    src.write_text("tmdb_person_id,name,qid,zh_labels,zh_aliases\n"
                   "29545,Claire Bloom,Q235622,克莱尔·布卢姆|克萊爾·布鲁姆,克萊兒·布魯姆\n"
                   "7383,John Longden,Q2748881,,\n"
                   "999999,Nobody,Q1,某人,\n", encoding="utf-8")
    assert mod.import_csv(lib, src) == {"rows": 3, "written": 1, "no_zh": 1, "not_in_db": 1}
    for q in ("布卢姆", "克萊兒", "Claire Bloom"):
        assert [r["id"] for r in lib.list_movies(q=q)] == [movie["id"]], q
    lib.set_person_aka(29545, ["Claire Bloom"])  # 重拉 TMDB 别名
    assert [r["id"] for r in lib.list_movies(q="布卢姆")] == [movie["id"]]


def test_search_folds_traditional_to_simplified(tmp_path, cfg, lib):
    """10-07：繁体搜简体、简体搜繁体都中（人名和片名同一套比对）。"""
    seed_tree(tmp_path, cfg, lib)
    movie = lib.list_movies()[0]
    lib.save_credits("movie", movie["id"], [{"id": 1, "name": "洪金宝", "role": "director", "ord": 0},
                                             {"id": 2, "name": "周潤發", "role": "cast", "ord": 1}])
    for q in ("洪金寶", "洪金宝", "周润发", "周潤發"):
        assert [r["id"] for r in lib.list_movies(q=q)] == [movie["id"]], q
