"""extras.py 补测：拉取失败/部分失败不丢已有数据、合集成员只补缺、ffprobe 探测出错。
一律假 TMDB 客户端、假 subprocess，虚构片名，不碰网络、不读真视频。"""

from __future__ import annotations

import json
import subprocess

import pytest

from homecinema import extras


def _quiet(*a, **k):
    pass


def _movie(lib, tmp_path, name, tmdb_id=None):
    p = tmp_path / "movies" / name / f"{name}.mp4"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"")
    mid = lib.upsert_movie(path=str(p), folder=str(p.parent), video_name=name, ext=".mp4",
                           title=name, year=None, size=0, mtime=0)
    if tmdb_id is not None:
        lib.update_movie_metadata(mid, {"tmdb_id": tmdb_id, "status": "matched"})
    return mid


def _show(lib, name, tmdb_id):
    sid = lib.upsert_show(path=f"/tv/{name}", folder=f"/tv/{name}", title=name, year=None)
    lib.update_show_metadata(sid, {"tmdb_id": tmdb_id, "status": "matched"})
    return sid


class FakeTmdb:
    """可按 tmdb_id 配置返回值或异常的假客户端；记录每次请求。"""

    def __init__(self, movies=None, shows=None, collections=None):
        self.movies = movies or {}
        self.shows = shows or {}
        self.collections = collections or {}
        self.calls = []

    def download_image(self, path, images_dir, kind="poster"):
        self.calls.append(("img", path, kind))
        return "img_" + path.strip("/")

    def _get(self, table, key):
        v = table.get(key)
        if isinstance(v, Exception):
            raise v
        return v

    def movie_detail_with(self, tmdb_id, append):
        self.calls.append(("movie", tmdb_id, append))
        return self._get(self.movies, tmdb_id)

    def tv_detail_with(self, tmdb_id, append):
        self.calls.append(("tv", tmdb_id, append))
        return self._get(self.shows, tmdb_id)

    def collection_detail(self, coll_id):
        self.calls.append(("coll", coll_id))
        return self._get(self.collections, coll_id)


def _movie_data(coll=None, cast=None):
    return {"credits": {"crew": [{"id": 11, "name": "Ann Director", "job": "Director"}],
                        "cast": cast if cast is not None else [{"id": 12, "name": "Cat Actor", "character": "Hero"}]},
            "belongs_to_collection": coll}


# ---------- run_extras：单条失败不中断、不写 credits_at ----------

def test_run_extras_partial_failure_keeps_going_and_retries_next_time(tmp_path, cfg, lib):
    """一部抛异常、一部返回空：都计 failed、不落 credits_at（下次续跑会再拉），其余照常落库。"""
    bad = _movie(lib, tmp_path, "Broken Reel", 1)
    empty = _movie(lib, tmp_path, "Empty Reel", 2)
    good = _movie(lib, tmp_path, "Good Reel", 3)
    tmdb = FakeTmdb(movies={1: RuntimeError("超时"), 2: None, 3: _movie_data()})
    logs = []
    stats = extras.run_extras(lib, tmdb, cfg.images_dir, progress=lambda m, **k: logs.append(m))
    assert stats == {"total": 3, "ok": 1, "failed": 2}
    assert any("Broken Reel 出错：RuntimeError: 超时" in m for m in logs)
    assert not any(m.endswith("Empty Reel") for m in logs)  # 空返回不报成功
    assert lib.get_movie(bad)["credits_at"] is None and lib.get_movie(empty)["credits_at"] is None
    assert lib.get_movie(good)["credits_at"] is not None
    movies, _ = lib.items_needing_extras()
    assert sorted(m["id"] for m in movies) == [bad, empty]


def test_refresh_failure_does_not_wipe_existing_credits(tmp_path, cfg, lib):
    """refresh 重拉时 TMDB 失败：旧演职员和合集不能被清掉。"""
    mid = _movie(lib, tmp_path, "Keep Reel", 5)
    coll = {"id": 70, "name": "Keep Saga", "poster_path": None}
    tmdb = FakeTmdb(movies={5: _movie_data(coll=coll)}, collections={70: {"parts": [{"id": 5, "title": "Keep Reel"}]}})
    extras.run_extras(lib, tmdb, cfg.images_dir, progress=_quiet)
    before = [dict(r) for r in lib.credits_for("movie", mid)]
    assert before and lib.get_movie(mid)["collection_id"] == 70
    tmdb.movies[5] = RuntimeError("断网")
    stats = extras.run_extras(lib, tmdb, cfg.images_dir, refresh=True, progress=_quiet)
    assert stats == {"total": 1, "ok": 0, "failed": 1}
    assert [dict(r) for r in lib.credits_for("movie", mid)] == before
    assert lib.get_movie(mid)["collection_id"] == 70


# ---------- 合集：成员只拉一次、失败保留旧值、事后补拉 ----------

def test_collection_parts_fetched_once_and_failure_backfilled_later(tmp_path, cfg, lib):
    """合集成员第一次拉失败（None）：parts 留空，run_extras 收尾补拉；拉到后同合集不再重复请求。"""
    a = _movie(lib, tmp_path, "Saga One", 101)
    coll = {"id": 80, "name": "Fake Saga", "poster_path": "/saga.jpg"}
    tmdb = FakeTmdb(movies={101: _movie_data(coll=coll)}, collections={80: None})
    extras.run_extras(lib, tmdb, cfg.images_dir, progress=_quiet)
    row = lib.get_collection(80)
    assert row["parts"] is None and row["poster_cached"] == "img_saga.jpg"
    assert lib.get_movie(a)["collection_id"] == 80
    # 片内拉一次 + 收尾补拉一次，都失败：parts 仍留空
    assert tmdb.calls.count(("coll", 80)) == 2

    tmdb.collections[80] = {"parts": [{"id": 101, "title": "Saga One", "release_date": "2010-02-03"},
                                      {"id": 102, "title": "Saga Two", "release_date": ""},
                                      {"title": "无 id 的成员被跳过"}]}
    tmdb.calls.clear()
    stats = extras.run_extras(lib, tmdb, cfg.images_dir, progress=_quiet)
    assert stats["total"] == 0 and tmdb.calls == [("coll", 80)]  # 没片要拉，也会收尾补拉合集
    row = lib.get_collection(80)
    assert json.loads(row["parts"]) == [{"tmdb_id": 101, "title": "Saga One", "year": 2010},
                                        {"tmdb_id": 102, "title": "Saga Two", "year": None}]
    assert row["poster_cached"] == "img_saga.jpg"  # 补拉成员不冲掉海报

    _movie(lib, tmp_path, "Saga Two", 102)
    tmdb.movies[102] = _movie_data(coll=dict(coll, poster_path=None))
    tmdb.calls.clear()
    extras.run_extras(lib, tmdb, cfg.images_dir, progress=_quiet)
    assert ("coll", 80) not in tmdb.calls  # 同合集已有成员：不重复请求
    assert lib.get_collection(80)["poster_cached"] == "img_saga.jpg"  # 第二部没海报：不覆盖已有海报

    tmdb.calls.clear()
    tmdb.collections[80] = None  # 已有成员时即便 TMDB 失败也不会再去拉、不会被冲掉
    extras.run_extras(lib, tmdb, cfg.images_dir, refresh=True, progress=_quiet)
    assert ("coll", 80) not in tmdb.calls
    assert len(json.loads(lib.get_collection(80)["parts"])) == 2


def test_parts_rejects_malformed_payload():
    """/collection 返回结构不对（parts 不是列表）视为失败，返回 None 而不是空列表（空列表会被当成「已拉过」）。"""
    assert extras._parts(None) is None
    assert extras._parts({"parts": None}) is None
    assert extras._parts({"parts": {"id": 1}}) is None
    assert extras._parts({"parts": []}) == []


def test_movie_without_collection_clears_stale_link(tmp_path, cfg, lib):
    """合集缺 id 视为无合集：collection_id 置空，不建合集行，也不请求 /collection。"""
    mid = _movie(lib, tmp_path, "Lone Reel", 9)
    tmdb = FakeTmdb(movies={9: _movie_data(coll={"name": "无 id"})})
    assert extras.fetch_movie_extras(lib, tmdb, cfg.images_dir, lib.get_movie(mid)) is True
    assert lib.get_movie(mid)["collection_id"] is None
    assert not any(c[0] == "coll" for c in tmdb.calls)


# ---------- _people：跳过残缺条目、只下有头像的、演员截断 ----------

def test_people_skips_incomplete_and_caps_cast(tmp_path, cfg, lib):
    mid = _movie(lib, tmp_path, "Crowd Reel", 20)
    cast = [{"id": None, "name": "无 id"}, {"id": 30, "name": ""}]
    cast += [{"id": 100 + i, "name": f"Extra {i}", "character": "" if i == 2 else f"Role {i}",
              "profile_path": "/p.jpg" if i == 2 else None} for i in range(15)]
    tmdb = FakeTmdb(movies={20: _movie_data(cast=cast)})
    extras.fetch_movie_extras(lib, tmdb, cfg.images_dir, lib.get_movie(mid))
    rows = [r for r in lib.credits_for("movie", mid) if r["role"] == "cast"]
    # 前 CAST_LIMIT 条里 2 条残缺被跳过 → 8 位演员
    assert [r["id"] for r in rows] == [100 + i for i in range(extras.CAST_LIMIT - 2)]
    by_id = {r["id"]: r for r in rows}
    assert by_id[102]["character"] is None  # 空角色名存成 NULL
    assert by_id[102]["profile_cached"] == "img_p.jpg" and by_id[100]["profile_cached"] is None
    assert [c for c in tmdb.calls if c[0] == "img"] == [("img", "/p.jpg", "profile")]


# ---------- 剧集：只刷播出信息时不动演员 ----------

def test_show_extras_roles_and_airing_only_refresh_keeps_credits(cfg, lib):
    sid = _show(lib, "Fake Series", 42)
    data = {"status": "Returning Series",
            "created_by": [{"id": 1, "name": "Ann Creator"}],
            "aggregate_credits": {"cast": [{"id": 2, "name": "No Role", "roles": []},
                                           {"id": 3, "name": "Lead", "roles": [{"character": "Captain"}]}]},
            "next_episode_to_air": {"season_number": 2, "episode_number": 1, "air_date": "2099-01-01"},
            "last_episode_to_air": {"season_number": 1, "episode_number": 8, "air_date": None}}
    tmdb = FakeTmdb(shows={42: data})
    assert extras.fetch_show_extras(lib, tmdb, cfg.images_dir, lib.get_show(sid)) is True
    chars = {r["name"]: r["character"] for r in lib.credits_for("show", sid)}
    assert chars == {"Ann Creator": None, "No Role": None, "Lead": "Captain"}
    show = lib.get_show(sid)
    assert json.loads(show["next_air"]) == {"season": 2, "episode": 1, "name": "", "air_date": "2099-01-01"}
    assert show["last_air"] is None  # 没播出日期不算
    credits_at = show["credits_at"]
    assert credits_at is not None

    # 追剧刷新：不带 aggregate_credits，演员保留，credits_at 不变
    tmdb.shows[42] = {"status": "Ended"}
    assert extras.fetch_show_extras(lib, tmdb, cfg.images_dir, lib.get_show(sid), with_credits=False)
    assert tmdb.calls[-1] == ("tv", 42, None)
    show = lib.get_show(sid)
    assert show["tmdb_status"] == "Ended" and show["next_air"] is None
    assert show["credits_at"] == credits_at
    assert len(lib.credits_for("show", sid)) == 3

    tmdb.shows[42] = None  # 拉取失败：返回 False，不写任何东西
    assert extras.fetch_show_extras(lib, tmdb, cfg.images_dir, lib.get_show(sid)) is False
    assert lib.get_show(sid)["tmdb_status"] == "Ended"


def test_refresh_airing_survives_single_failure_and_skips_ended(cfg, lib):
    """一部剧抛异常不影响其他剧；已完结的不再请求；progress 每部都回调。"""
    ended = _show(lib, "Old Series", 1)
    lib.set_show_airing(ended, "Ended", None, None)
    bad = _show(lib, "Bad Series", 2)
    ok = _show(lib, "Live Series", 3)
    tmdb = FakeTmdb(shows={2: ValueError("坏 JSON"), 3: {"status": "Returning Series"}})
    seen = []
    n = extras.refresh_airing(lib, tmdb, cfg.images_dir, progress=lambda i, t, title: seen.append((i, t, title)))
    assert n == 2
    assert seen == [(1, 2, "Bad Series"), (2, 2, "Live Series")]
    assert ("tv", 1, None) not in tmdb.calls
    assert lib.get_show(ok)["tmdb_status"] == "Returning Series"
    assert lib.get_show(bad)["tmdb_status"] is None
    assert extras.refresh_airing(lib, FakeTmdb(shows={2: None, 3: None}), cfg.images_dir) == 2  # 无 progress 也可


# ---------- 人名别名 ----------

def test_run_people_aka_exception_counts_failed_and_progress(lib):
    lib.save_credits("movie", 1, [{"id": 7, "name": "Ann", "role": "cast", "ord": 0},
                                  {"id": 8, "name": "Bob", "role": "cast", "ord": 1}])

    class Client:
        def person_detail(self, pid):
            if pid == 7:
                raise TimeoutError("超时")
            return {"id": 8, "name": " Bob ", "also_known_as": ["", None, "'Bob'", "鲍勃"]}

    logs = []
    stats = extras.run_people_aka(lib, Client(), progress=lambda m, **k: logs.append(m))
    assert stats == {"total": 2, "ok": 1, "failed": 1}
    assert lib.people_needing_aka() == [7]
    assert lib.get_person(8)["aka"] == "Bob\n鲍勃"
    assert logs == ["[人名 2/2]"]
    assert extras.person_aka({"name": "无 id"}) is None


# ---------- ffprobe 分辨率 ----------

def test_probe_heights_handles_errors_and_only_fills_missing(tmp_path, lib, monkeypatch):
    """各种 ffprobe 出错都记 None 不中断；已有高度、下架的片不探测。"""
    outs = {}
    for name, result in [("A Ok", "1080\n"), ("B Na", "N/A\n"), ("C Empty", ""),
                         ("D Timeout", subprocess.TimeoutExpired("ffprobe", 60)),
                         ("E NoBin", FileNotFoundError("ffprobe")), ("F Multi", "2160\n720\n")]:
        outs[_movie(lib, tmp_path, name)] = result
    have = _movie(lib, tmp_path, "G Have")
    lib.set_video_height(have, 480)
    gone = _movie(lib, tmp_path, "H Gone")
    lib.mark_missing_movies({lib.get_movie(m)["path"] for m in list(outs) + [have]})

    by_path = {lib.get_movie(m)["path"]: r for m, r in outs.items()}
    probed = []

    def fake_run(cmd, **kw):
        probed.append(cmd[-1])
        r = by_path[cmd[-1]]
        if isinstance(r, BaseException):
            raise r
        return subprocess.CompletedProcess(cmd, 0, stdout=r, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    logs = []
    assert extras.probe_heights(lib, progress=lambda m, **k: logs.append(m)) == 6
    assert sorted(probed) == sorted(by_path)  # 没探测已有高度的和下架的
    got = {lib.get_movie(m)["title"]: lib.get_movie(m)["video_height"] for m in outs}
    assert got == {"A Ok": 1080, "B Na": None, "C Empty": None, "D Timeout": None, "E NoBin": None, "F Multi": 2160}
    assert lib.get_movie(have)["video_height"] == 480
    assert lib.get_movie(gone)["video_height"] is None
    assert logs[0] == "[1/6] A Ok 1080"
