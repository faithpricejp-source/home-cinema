"""审计复现测试：F01-F05 见 AUDIT.md；N01-N04 见 SUSPECTS.md「来源 1」（db.py / metadata.py）。

测试断言的是**正确行为**，在未修复的代码上应失败（失败即复现）。
外部接口一律用假客户端/monkeypatch 注入，不联网、不写 ~/ 真实路径。见 ../../SUSPECTS.md。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from homecinema import ocr_credits as oc
from homecinema import subtitles as sm
from homecinema.community import CommunityClient, run_community
from homecinema.metadata import MetadataService, TmdbClient


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
# SUSPECTS.md「来源 1」：db.py / metadata.py，编号 N01-N04
# ==================================================================

TV_JSON = {
    "id": 202, "overview": "剧集简介", "name": "虚构剧",
    "genres": [{"id": 3, "name": "悬疑"}], "vote_average": 8.2,
    "episode_run_time": [45], "poster_path": None, "backdrop_path": None,
    "seasons": [{"season_number": 1, "name": "第 1 季", "episode_count": 1,
                 "poster_path": None}],
}


class _FakeTmdb:
    """只实现 enrich_show 用到的方法；season_detail 可切换成失败。"""

    def __init__(self, season_fail: bool = False):
        self.season_fail = season_fail

    def find_by_imdb(self, imdb_id, kind):
        return None

    def search_movie(self, title, year=None):
        return None

    def search_tv(self, title, year=None):
        return {"id": 202}

    def movie_detail(self, tmdb_id, language=None):
        return None

    def tv_detail(self, tmdb_id, language=None):
        return dict(TV_JSON)

    def season_detail(self, tv_id, season_number, language=None):
        if self.season_fail:
            return None
        return {"name": "第 1 季", "episodes": [
            {"episode_number": 1, "id": 9001, "name": "TMDB-E1",
             "still_path": None, "runtime": 45, "overview": "第一集简介",
             "air_date": "2020-01-01"}]}

    def download_image(self, *a, **k):
        return None


# ---------- N01 ----------

def test_N01_save_credits_failed_batch_keeps_old_credits(lib):
    """save_credits 是「整批替换」：批量中途失败不得丢掉旧演职员。

    现状：DELETE 已进未提交事务，异常后不回滚；之后任意一次落库的 commit
    （run_extras 对下一条电影的写入）都会把旧数据一起冲掉。"""
    lib.save_credits("movie", 1, [
        {"id": 11, "name": "导演甲", "role": "director", "ord": 0},
        {"id": 12, "name": "演员乙", "role": "cast", "character": "主角", "ord": 1},
    ])
    with pytest.raises(KeyError):
        lib.save_credits("movie", 1, [
            {"id": 13, "name": "演员丙", "role": "cast", "character": "配角", "ord": 0},
            {"id": 14},  # 缺 name：批量中途失败
        ])
    # 失败之后业务照常继续，任意一次落库都会 commit
    lib.save_playback("movie", 1, 5.0, 100.0, False)
    assert {r["id"] for r in lib.credits_for("movie", 1)} == {11, 12}


# ---------- N02 ----------

def test_N02_failed_season_detail_is_retried_on_next_run(lib, tmp_path):
    """enrich_show 里 season_detail 失败被 continue 吞掉，剧照样标 matched：
    集元数据永久漏，之后每次默认补元数据都跳过这部剧。

    正确行为：网络恢复后再跑一次默认的 enrich_all，集元数据要能补上。"""
    sid = lib.upsert_show(path="/tv/虚构剧", folder="/tv/虚构剧",
                          title="虚构剧", year=2020)
    eid = lib.upsert_episode(show_id=sid, season_number=1, episode_number=1,
                             path="/tv/虚构剧/S01E01.mkv", video_name="S01E01.mkv",
                             ext="mkv", title="S01E01", size=1, mtime=1.0)
    client = _FakeTmdb(season_fail=True)
    svc = MetadataService(lib, client, tmp_path / "img")
    svc.enrich_all()  # 本轮 season_detail 失败

    client.season_fail = False
    svc.enrich_all()  # 下一轮默认补元数据（不含 --refresh）
    assert lib.get_episode(eid)["title"] == "TMDB-E1"


# ---------- N03 ----------

class _SimulatedKill(Exception):
    """模拟进程在写文件中途被杀。"""


def test_N03_download_image_does_not_serve_truncated_cache(tmp_path, monkeypatch):
    """download_image 的缓存写入不是原子的，且命中判断只看文件存在：
    写到一半进程被杀会留下截断文件，之后被永久当作有效缓存返回。

    正确行为：失败不留下会被当成功的坏文件（原子写或失败清理），重下后内容完整。"""
    images = tmp_path / "images"
    payload = b"x" * 1000

    class _Http:
        def __init__(self):
            self.n = 0

        def get(self, url, params=None, timeout=None):
            self.n += 1
            class _R:
                status_code = 200
                content = payload
            return _R()

    http = _Http()
    client = TmdbClient("k", http=http, sleep=lambda s: None, clock=lambda: 0.0,
                        min_interval=0)

    real_write = Path.write_bytes

    def half_write(self, data):
        real_write(self, data[: len(data) // 2])  # 写到一半
        raise _SimulatedKill                       # 进程被杀

    monkeypatch.setattr(Path, "write_bytes", half_write)
    with pytest.raises(_SimulatedKill):
        client.download_image("/poster.jpg", images)

    monkeypatch.undo()  # 进程重启，缓存/下载逻辑恢复正常
    name = client.download_image("/poster.jpg", images)
    assert http.n == 2  # 截断文件不能当缓存命中，必须真的重新下载
    assert (images / name).read_bytes() == payload


# ---------- N04 ----------

@pytest.mark.xfail(strict=True, reason="跳过：仅「继续观看」首推卡口径不一致（低），"
                                       "非崩溃/丢数据/漏处理，代价大于收益")
def test_N04_first_episode_skips_specials(lib):
    """first_episode 不排除季 0 特别篇：「继续观看」对从未看过的剧指向 S00E01。

    正确行为：与 latest_local_episode / missing_episodes 的口径一致，
    第一集从正片（季号>0）算起。"""
    sid = lib.upsert_show(path="/tv/虚构剧2", folder="/tv/虚构剧2",
                          title="虚构剧2", year=2020)
    for sn, en in [(0, 1), (1, 1), (1, 2)]:
        lib.upsert_episode(show_id=sid, season_number=sn, episode_number=en,
                           path=f"/tv/虚构剧2/S{sn:02d}E{en:02d}.mkv",
                           video_name=f"S{sn:02d}E{en:02d}.mkv",
                           ext="mkv", title=f"E{en}", size=1, mtime=1.0)
    fe = lib.first_episode(sid)
    assert (fe["season_number"], fe["episode_number"]) == (1, 1)
