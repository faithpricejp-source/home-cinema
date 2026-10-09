"""审计复现测试：F01-F05 见 AUDIT.md；N01-N05 见 SUSPECTS.md「来源 2」（server.py）。

测试断言的是**正确行为**，在未修复的代码上应失败（失败即复现）。
外部接口一律打桩：不发真实网络请求、不起 ffprobe/IINA 子进程、不写 ~/ 真实路径。
见 ../../SUSPECTS.md。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from homecinema import ocr_credits as oc
from homecinema import subtitles as sm
from homecinema import subtitles as subs_mod
from homecinema.community import CommunityClient, run_community
from homecinema.player import Player
from homecinema.server import ScanService, create_app


# ---------- 假 HTTP（community.py 用） ----------

class Resp:
    def __init__(self, code, data=None):
        self.status_code = code
        self._data = data

    def json(self):
        return self._data


class BadJsonResp:
    """HTTP 200 但响应体不是 JSON（网关/CDN 错误页等）。"""

    status_code = 200

    def json(self):
        raise ValueError("Expecting value: line 1 column 1 (char 0)")


class FakeHttp:
    def __init__(self, routes):
        self.routes = routes  # [(url 子串, 参数子集, Resp)]
        self.calls = []

    def get(self, url, params=None, headers=None):
        self.calls.append((url, dict(params or {})))
        for sub, want, resp in self.routes:
            if sub in url and all(params.get(k) == v for k, v in want.items()):
                return resp
        return Resp(404, {"error": "media not found"})


def community_client(routes):
    return CommunityClient(http=FakeHttp(routes), sleep=lambda s: None, min_interval=0)


def _one_episode_show(lib, title, tmdb_id):
    sid = lib.upsert_show(path=f"/tv/{title}", folder=f"/tv/{title}", title=title, year=None)
    lib.update_show_metadata(sid, {"tmdb_id": tmdb_id, "status": "matched"})
    eid = lib.upsert_episode(show_id=sid, season_number=1, episode_number=1,
                             path=f"/tv/{title}/a.mkv", video_name="a", ext=".mkv",
                             title="a", size=1, mtime=0)
    return eid


# ---------- F01 ----------

def test_F01_200_with_bad_json_is_retryable_error_not_permanent_none(lib, monkeypatch):
    """TheIntroDB 返回 200 但响应体不是 JSON：当前被当成「没有数据」写进库，永久跳过；
    正确行为是按可重试错误处理，不落库，下次重跑再查。"""
    eid = _one_episode_show(lib, "Bad JSON Show", 888)
    c = community_client([("theintrodb", {}, BadJsonResp())])
    monkeypatch.setattr("homecinema.community._duration", lambda ep, d: 1500.0)
    stats = run_community(lib, c, tmdb=None, progress=lambda *a, **k: None)
    assert lib.get_community_segment(eid) is None      # 当前代码：已写入 status='none'
    assert stats["error"] == 1 and stats["none"] == 0  # 当前代码：error=0、none=1


# ---------- ocr_credits 帧构造（沿用 tests/test_ocr_credits.py 的约定） ----------

def frame(t, dark=0.95, lines=()):
    return {"t": t, "dark": dark, "lines": [list(x) for x in lines]}


def story(t0, t1):
    sub = (("我为什么要听你的", 0.3, 0.02, 0.4, 0.03),)
    return [frame(t, dark=0.3, lines=sub) for t in range(t0, t1, 2)]


def credits(t0, t1):
    # 每帧名字不同（同一串字出现在三成以上的帧里会被当成台标）
    return [frame(t, lines=((f"Producer Role{chr(65 + t % 26)}", 0.4, 0.55, 0.2, 0.03),
                            (f"PERSON NAME{chr(65 + t % 26)}{chr(65 + t // 26 % 26)}", 0.4, 0.47, 0.2, 0.05)))
            for t in range(t0, t1, 2)]


def episode(frames, duration=1400.0, start=1100.0):
    return {"duration": duration, "start": start, "step": 2.0, "frames": frames}


# ---------- F02 ----------

def test_F02_to_utf8_decodes_big5_correctly():
    """繁体（Big5）字幕：docstring 承诺「GBK/BIG5/UTF-16 都有」都要转成 UTF-8，
    但 gb18030 排在 big5 前且能解开几乎所有 big5 字节 → big5 分支永远走不到，结果是乱码。"""
    text = "片尾字幕：感謝所有工作人員的付出，我們下次再見！\n"
    data = text.encode("big5")
    assert sm.to_utf8(data).decode("utf-8") == text


# ---------- F03 ----------

def test_F03_dark_scene_chinese_dialog_with_fullwidth_punct_not_credits():
    """暗场对白字幕以全角 ？/！ 结尾：_DIALOG 只认半角 ?/!（全角省略号 … 却认），
    中文字幕整行漏判 → 被当成演职员表，与片尾块连成一块，起点提前到对白处。"""
    dialog = [(("有人从帕萨迪纳来，", 0.3, 0.2, 0.4, 0.04), ("加州的，他说", 0.3, 0.16, 0.4, 0.04),
               ("他说他一定会回来？", 0.3, 0.02, 0.4, 0.03)),
              (("你到底在做什么？", 0.3, 0.2, 0.4, 0.04), ("你不是说过吗！", 0.3, 0.16, 0.4, 0.04),
               ("那个人是谁", 0.3, 0.02, 0.4, 0.04))]
    ep = episode(story(1101, 1281)
                 + [frame(t, dark=0.8, lines=dialog[t // 2 % 2]) for t in range(1281, 1301, 2)]
                 + credits(1301, 1399))
    assert oc.decide(ep) == 1300.0


# ---------- F04 ----------

def test_F04_null_item_in_credits_list_does_not_abort_run(lib, monkeypatch):
    """TheIntroDB 200 响应里 credits 列表含 null 项：theintrodb() 抛 AttributeError
    （既不是 RuntimeError 也不是 QuotaExceeded），绕过 run_community 的逐集容错，
    整轮查询命令崩掉。正确行为：null 项当无效条目跳过，有效的 intro 照常落库。"""
    eid = _one_episode_show(lib, "Null Credits Show", 889)
    c = community_client([("theintrodb", {}, Resp(200, {
        "intro": [{"start_ms": 1000, "end_ms": 40000}],
        "credits": [None]}))])
    monkeypatch.setattr("homecinema.community._duration", lambda ep, d: 1500.0)
    stats = run_community(lib, c, tmdb=None, progress=lambda *a, **k: None)
    assert stats["found"] == 1 and stats["error"] == 0
    row = lib.get_community_segment(eid)
    assert row["provider"] == "theintrodb" and row["intro_end"] == 40.0


# ---------- F05 ----------

def test_F05_invalid_config_path_gets_clean_error_not_traceback(capsys):
    """--config 指向带父目录组件的路径是 norm_path **按设计拒绝**的输入类，
    main() 却只接 FileNotFoundError：ValueError 裸奔成 traceback。
    正确行为：与「找不到配置文件」同样干净地报错并返回 2。"""
    from homecinema.__main__ import main
    rc = main(["--config", "../x.toml", "scan"])
    assert rc == 2
    assert "错误" in capsys.readouterr().err


# ==================================================================
# SUSPECTS.md「来源 2」：server.py，编号 N01-N05
# ==================================================================

def seed_movie(lib, folder_name, video_name, title, year=2009,
               ext="mp4", size=1000, tmdb=None, height=None):
    folder = f"/lib-fixture/{folder_name}"
    mid = lib.upsert_movie(path=f"{folder}/{video_name}", folder=folder,
                           video_name=video_name, ext=ext, title=title, year=year,
                           size=size, mtime=1.0)
    if height is not None:
        lib.set_video_height(mid, height)
    if tmdb is not None:
        lib.update_movie_metadata(mid, {"tmdb_id": tmdb, "status": "matched"})
    return mid


def seed_show(lib, title, year=2020):
    folder = f"/tv-fixture/{title}"
    sid = lib.upsert_show(path=folder, folder=folder, title=title, year=year)
    return sid


def seed_episode(lib, sid, sn, en, show_title):
    return lib.upsert_episode(show_id=sid, season_number=sn, episode_number=en,
                              path=f"/tv-fixture/{show_title}/S{sn:02d}E{en:02d}.mkv",
                              video_name=f"{show_title} S{sn:02d}E{en:02d}.mkv",
                              ext="mkv", title=f"E{en}", size=1, mtime=1.0)


@pytest.fixture
def no_probe(monkeypatch):
    """embedded_status 会起 ffprobe 子进程；这里一律打桩，测试不许起子进程。"""
    monkeypatch.setattr(subs_mod, "embedded_status", lambda path, whole_folder=False:
                        {"embedded": [], "external": [], "has_zh": False})


@pytest.fixture
def client(cfg, lib, no_probe):
    player = Player(lib, cfg, launcher=lambda *a, **k: None)
    app = create_app(config=cfg, db=lib, player=player, scan_service=ScanService(cfg, lib))
    with TestClient(app) as tc:
        yield tc


# ---------- N01 ----------

def test_N01_assrt_transport_error_is_502_not_500(client, cfg, lib, monkeypatch):
    """射手网连接失败（超时/断网等传输层错误）与上游报错同样属于「上游不可用」，
    两个字幕端点都该回 502 + detail，而不是裸 500 Internal Server Error。"""
    sid = seed_show(lib, "Probe Show")
    eid = seed_episode(lib, sid, 1, 1, "Probe Show")
    Path(cfg.assrt_token_file).write_text("dummy-token", encoding="utf-8")

    class BoomClient:
        def __init__(self, token):
            pass

        def search(self, q, is_file=False):
            raise OSError("connection reset by peer")

        def detail(self, sub_id):
            raise OSError("connection reset by peer")

    monkeypatch.setattr(subs_mod, "AssrtClient", BoomClient)

    r1 = client.get("/api/subtitles/search", params={"type": "episode", "id": eid})
    assert r1.status_code == 502, f"search 实际返回 {r1.status_code}: {r1.text}"

    r2 = client.post("/api/subtitles/download",
                     json={"type": "episode", "id": eid, "sub_id": 7})
    assert r2.status_code == 502, f"download 实际返回 {r2.status_code}: {r2.text}"


# ---------- N02 ----------

def test_N02_movie_detail_favorite_covers_whole_version_group(client, lib):
    """列表卡片语义：组内任一版本被收藏 = 这部电影被收藏（_filter_items 注释）。
    详情页的 favorite 也代表这部电影，不能只看当前查看的那个文件。"""
    a = seed_movie(lib, "Watchmen (2009)", "Watchmen (2009).mp4", "Watchmen",
                   size=2000, tmdb=13183, height=2160)  # 默认版本（4K）
    b = seed_movie(lib, "Watchmen (2009) Ultimate Cut",
                   "Watchmen (2009) Ultimate Cut.mkv", "Watchmen Ultimate Cut",
                   ext="mkv", size=4000, tmdb=13183, height=1080)  # 非默认版本

    assert client.post("/api/favorite",
                       json={"type": "movie", "id": b, "favorite": True}).status_code == 200

    card = client.get("/api/movies").json()[0]
    assert card["favorite"] is True  # 前置：卡片已经按整组算

    assert client.get(f"/api/movie/{a}").json()["favorite"] is True


# ---------- N03 ----------

@pytest.mark.xfail(strict=True, reason="跳过：只增长磁盘垃圾（subs 目录只增不减），"
                                       "不丢数据、不漏处理，优先级低于已修条目")
def test_N03_subtitle_remove_deletes_file(client, cfg, lib):
    """详情页「移除」是把这份字幕从片库里去掉：文件也应一并删除，
    否则 subs/<type>-<id>/ 里的文件成为永久不可见的垃圾（目录只增不减）。"""
    sid = seed_show(lib, "Probe Show")
    eid = seed_episode(lib, sid, 1, 1, "Probe Show")
    sub_dir = cfg.subs_dir / f"episode-{eid}"
    sub_dir.mkdir(parents=True, exist_ok=True)
    sub_file = sub_dir / "Probe Show S01E01.9.zh.srt"
    sub_file.write_text("1\n00:00:01,000 --> 00:00:02,000\n测试\n", encoding="utf-8")
    lib.add_subtitle("episode", eid, str(sub_file), 9)

    r = client.post("/api/subtitles/remove",
                    json={"type": "episode", "id": eid, "name": sub_file.name})
    assert r.status_code == 200
    assert not sub_file.exists()


# ---------- N04 ----------

@pytest.mark.xfail(strict=True, reason="跳过：missing=1 重扫即自愈的显示错（低），"
                                       "且修法要动 movies_in_collection 口径，波及 /api/collections 计数")
def test_N04_collection_counts_owned_movie_even_while_missing(client, lib):
    """missing=1 = 文件暂时不在位（卷没挂时扫过一轮），片子仍然是「已拥有」——
    推荐模块就是这么算的（all_tmdb_ids：含 missing=1，仍算已拥有）。
    合集页的 missing（未收录）不能把自有片也列进去，勾引用户去买/下已有的片。"""
    m1 = seed_movie(lib, "Coll A (2001)", "Coll A (2001).mp4", "Coll A", tmdb=100)
    m2 = seed_movie(lib, "Coll B (2002)", "Coll B (2002).mp4", "Coll B", tmdb=200)
    lib.upsert_collection(777, "Test Coll", None,
                          [{"tmdb_id": 100, "title": "Coll A", "year": 2001},
                           {"tmdb_id": 200, "title": "Coll B", "year": 2002},
                           {"tmdb_id": 300, "title": "Coll C", "year": 2003}])
    lib.set_movie_extras(m1, 777)
    lib.set_movie_extras(m2, 777)
    # m2 的文件丢了（卷没挂时扫过一轮）→ missing=1；m1 仍在架
    lib.mark_missing_movies({f"/lib-fixture/Coll A (2001)/Coll A (2001).mp4"})

    d = client.get("/api/collection/777").json()
    missing_ids = {p["tmdb_id"] for p in d["missing"]}
    assert missing_ids == {300}, f"missing 列表实际是 {missing_ids}，自有片 200 被当成未收录"


# ---------- N05 ----------

@pytest.mark.xfail(strict=True, reason="跳过：只影响响应体冗余（内部 items 字段外泄），"
                                       "前端不消费、不丢数据，不属五类后果")
def test_N05_people_payload_does_not_leak_internal_items(client, lib):
    """`people_by_role` 用 `{**a, "count": len(a.pop("items"))}` 想把内部 items 聚合
    从 payload 里删掉，但 `{**a}` 先展开、pop 后才求值，items 原样留在响应里。
    端点契约是 {id, name, profile, count}（app.js 只消费这四个）。"""
    a = seed_movie(lib, "Person Movie (2010)", "Person Movie (2010).mp4",
                   "Person Movie", year=2010, tmdb=55)
    lib.save_credits("movie", a, [{"id": 9, "name": "Some Director", "profile_path": None,
                                   "profile_cached": None, "role": "director",
                                   "character": None, "ord": 0}])
    people = client.get("/api/people").json()
    assert len(people) == 1
    assert set(people[0]) == {"id", "name", "profile", "count"}, \
        f"实际字段：{sorted(people[0])}"
