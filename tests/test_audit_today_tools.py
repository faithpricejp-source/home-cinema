"""今日改动（多版本分组 / 口味重排）与 tools 的审计复现测试，对应 AUDIT.md 的 T01–T06。

每个测试断言「正确行为」，在当前代码上必须失败；外部接口一律注入假函数，不发真实请求。
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from homecinema import rerank as rerank_mod
from homecinema.db import Library
from homecinema.player import Player
from homecinema.server import ScanService, create_app
from test_versions import seed_movie

ROOT = Path(__file__).resolve().parents[1]


def load_tool(name: str):
    """按文件路径加载 tools/<name>.py（tools 不是包）。"""
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def client(cfg, lib):
    player = Player(lib, cfg, launcher=lambda *a, **k: None,
                    poll_interval=0.05, connect_deadline=1.0)
    app = create_app(config=cfg, db=lib, player=player, scan_service=ScanService(cfg, lib))
    with TestClient(app, base_url="http://127.0.0.1") as tc:
        yield tc


# ---------- T01 收藏筛选只看组内默认版本 ----------

def test_T01_favorite_filter_covers_whole_version_group(client, lib):
    """收藏同片任一版本 = 收藏这部电影；卡片是整部片，不是单个文件。"""
    a = seed_movie(lib, "Watchmen (2009)", "Watchmen (2009).mp4", "Watchmen",
                   size=2000, tmdb=13183, height=2160)  # 组内默认版本（4K、没播过）
    b = seed_movie(lib, "Watchmen (2009) Ultimate Cut",
                   "Watchmen (2009) Ultimate Cut.mkv", "Watchmen Ultimate Cut",
                   ext="mkv", size=4000, tmdb=13183, height=1080)  # 非默认版本

    assert [it["id"] for it in client.get("/api/movies").json()] == [a]  # 前置：默认版本是 a

    assert client.post("/api/favorite",
                       json={"type": "movie", "id": b, "favorite": True}).status_code == 200

    favs = client.get("/api/movies", params={"fav": "true"}).json()
    assert [it["id"] for it in favs] == [a]  # 收藏页不能把这部片整个吞掉

    card = client.get("/api/movies").json()[0]
    assert card["favorite"] is True  # 卡片上的收藏标记也要跟上


# ---------- T02 搜索命中组内一个版本 → 版本数/默认版本按子集算 ----------

def test_T02_search_returns_whole_group_not_only_matching_version(lib):
    """q 只命中组里一个版本时，分组信息仍属于整组：徽标版本数、默认版本都不该变。"""
    a = seed_movie(lib, "Watchmen (2009)", "Watchmen (2009).mp4", "Watchmen",
                   size=2000, tmdb=13183, height=2160)  # 整组默认版本（4K）
    b = seed_movie(lib, "Watchmen (2009) Ultimate Cut",
                   "Watchmen (2009) Ultimate Cut.mkv", "Watchmen Ultimate Cut",
                   ext="mkv", size=4000, tmdb=13183, height=1080)

    groups = lib.list_movie_groups(q="ultimate")  # 只有 b 的片名带 ultimate
    assert len(groups) == 1
    assert groups[0]["count"] == 2  # 「2 个版本」是这部电影的属性，不随搜索词变
    assert groups[0]["row"]["id"] == a  # 默认版本仍从整组挑（分辨率优先）


# ---------- T03 口味画像把「文件数」当「电影数」 ----------

def test_T03_taste_profile_counts_movies_not_files(lib):
    """画像发给外部 LLM，语义是「我收藏了几部这位导演的片、每个年代几部」；
    同片多版本不能把统计灌水。"""
    a = seed_movie(lib, "Inception (2010)", "Inception (2010).mp4", "Inception",
                   year=2010, size=2000, tmdb=27205, height=1080)
    b = seed_movie(lib, "Inception (2010) IMAX", "Inception (2010) IMAX.mkv",
                   "Inception IMAX", year=2010, ext="mkv", size=4000,
                   tmdb=27205, height=2160)  # 同一部片（tmdb 27205）的第二个版本
    c = seed_movie(lib, "Tenet (2020)", "Tenet (2020).mkv", "Tenet",
                   year=2020, ext="mkv", size=1000, tmdb=1571, height=1080)
    nolan = {"id": 1000, "name": "Christopher Nolan", "profile_path": None,
             "profile_cached": None, "role": "director", "character": None, "ord": 0}
    for mid in (a, b, c):  # 每个在架文件都会被 extras 各写一遍演职员
        lib.save_credits("movie", mid, [dict(nolan)])

    profile = rerank_mod.taste_profile(lib)
    assert profile["total_movies"] == 2  # 三部文件、两部片
    films = {d["name"]: d["films"] for d in profile["directors"]}
    assert films["Christopher Nolan"] == 2  # 导过两部片，不是三个文件
    eras = {e["decade"]: e["films"] for e in profile["eras"]}
    assert eras["2010s"] == 1  # 2010 年代只有《Inception》一部


# ---------- T04/T05 merge_segments ----------

def segments_db(tmp_path) -> tuple[Path, int]:
    """临时库里的一个剧集一集（返回库路径与集 id）。"""
    db_file = tmp_path / "library.db"
    lib = Library(db_file)
    sid = lib.upsert_show(path="/tv/The Loop (2020)", folder="/tv/The Loop (2020)",
                          title="The Loop", year=2020)
    eid = lib.upsert_episode(show_id=sid, season_number=1, episode_number=1,
                             path="/tv/The Loop (2020)/Season 01/The Loop S01E01.mkv",
                             video_name="The Loop S01E01.mkv", ext="mkv",
                             title="The Loop S01E01", size=1000, mtime=1.0)
    lib.close()
    return db_file, eid


def seed_segment_rows(db_file, eid, *, source, credits, runtime_minutes,
                      c_status="ok", c_credits=None, c_intro=None) -> None:
    """落 segments + community_segments（community_segments 没有 Library 写接口，用裸 SQL）。"""
    intro_start, intro_end = c_intro or (None, None)
    con = sqlite3.connect(db_file)
    con.execute("UPDATE episodes SET runtime_minutes=? WHERE id=?", (runtime_minutes, eid))
    con.execute("INSERT INTO segments (episode_id, intro_start, intro_end, credits_start,"
                " credits_end, source, detected_at) VALUES (?, NULL, NULL, ?, ?, ?, ?)",
                (eid, credits[0], credits[1], source, "2026-10-01T00:00:00+00:00"))
    con.execute("INSERT INTO community_segments (episode_id, provider, status, intro_start,"
                " intro_end, credits_start, credits_end, fetched_at)"
                " VALUES (?, 'theintrodb', ?, ?, ?, ?, NULL, ?)",
                (eid, c_status, intro_start, intro_end, c_credits, "2026-10-01T00:00:00+00:00"))
    con.commit()
    con.close()


@pytest.fixture
def merge(tmp_path, monkeypatch):
    mod = load_tool("merge_segments")
    monkeypatch.setattr(mod, "DB", str(tmp_path / "library.db"))
    monkeypatch.setattr(mod, "CACHE", str(tmp_path / "ocr"))
    monkeypatch.setattr(mod, "VLM", str(tmp_path / "vlm_results.jsonl"))
    return mod, tmp_path


def test_T04_cdb_tag_must_not_block_later_ocr_result(merge, monkeypatch):
    """片尾 tag 该只管自己那一源（片头的 cdb-intro 就是只管自己）：
    先用社区库补了片尾，之后文字识别跑出更准的值，重跑合并要能覆盖。"""
    ms, tmp_path = merge
    db_file, eid = segments_db(tmp_path)
    seed_segment_rows(db_file, eid, source="fingerprint+cdb", credits=(1000.0, 1800.0),
                      runtime_minutes=45, c_credits=1000.0)  # 第一次合并只吃到了社区库值

    (tmp_path / "ocr").mkdir(parents=True, exist_ok=True)  # 之后才有文字识别缓存
    (tmp_path / "ocr" / f"{eid}.json").write_text(
        json.dumps({"duration": 2400.0, "frames": []}), encoding="utf-8")
    monkeypatch.setattr(ms.oc, "decide", lambda _data: 2000.0)

    changes = ms.plan()
    assert [c["eid"] for c in changes] == [eid]
    assert changes[0]["credits"][0] == 2000.0  # 文字识别值覆盖社区库值
    assert "+ocr" in changes[0]["source"]


def test_T05_merged_credits_end_must_not_be_before_start(merge):
    """dur 取的是 TMDB 名义时长（runtime_minutes×60），常短于实际文件与片尾起点；
    写进 segments 的片尾段不能 end < start。"""
    ms, tmp_path = merge
    db_file, eid = segments_db(tmp_path)
    seed_segment_rows(db_file, eid, source="fingerprint", credits=(1000.0, 1750.0),
                      runtime_minutes=30, c_credits=2000.0)  # 名义 30 分钟，社区库片尾在 2000 秒

    changes = ms.plan()
    assert [c["eid"] for c in changes] == [eid]
    start, end = changes[0]["credits"]
    assert end >= start  # 当前写的是 (2000.0, 1800.0)


# ---------- T06 audit_matches ----------

def test_T06_audit_matches_must_not_call_api_failure_suspicious(cfg, lib, monkeypatch, capsys):
    """回查失败（401/429/5xx 都带 JSON 错误体）时没有任何可比对的片名，
    不能判成「可疑匹配」——这份 TSV 是人工纠错的输入。"""
    am = load_tool("audit_matches")
    seed_movie(lib, "Watchmen (2009)", "Watchmen (2009).mp4", "Watchmen",
               size=2000, tmdb=13183)

    class Resp:
        def json(self):
            return {"status_code": 13, "status_message": "Session token not found or invalid"}

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def get(self, url, params=None, timeout=None):
            return Resp()

    monkeypatch.setattr(am, "load_config", lambda *_a, **_k: cfg)
    monkeypatch.setattr(am, "httpx", types.SimpleNamespace(Client=Client, HTTPError=Exception))
    monkeypatch.setattr(am.time, "sleep", lambda _s: None)

    am.main()
    rows = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("movie\t")]
    assert len(rows) == 1
    fields = rows[0].split("\t")
    assert fields[4] == "" and fields[5] == ""  # 前置：TMDB 片名压根没拿到
    assert fields[-1] != "suspicious"  # 失败要标成查不到/错误，不是「匹配可疑」
