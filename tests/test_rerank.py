"""按口味重排测试：profile 隐私、批次切分、解析失败兜底、排序公式、关闭时零影响。

一律注入假 llm_call，不发真实请求。复用 test_recommend 的假 HTTP 客户端。
"""

from __future__ import annotations

import inspect
import json
import logging
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from homecinema.db import Library
from homecinema.recommend import generate
from homecinema.rerank import (FIT_WEIGHT, SCORE_WEIGHT, build_prompt,
                               rerank_movies, taste_profile)
from test_recommend import FakeResponse, add_movie, add_show, make_tmdb, movie_cand, tv_cand

ID_RE = re.compile(r'"tmdb_id"\s*:\s*(\d+)')


class FakeLLM:
    """记录每批 prompt；fits 之外的候选按 fit=5；reply 给定时原样返回（可传函数）。"""

    def __init__(self, fits=None, reply=None):
        self.calls = 0
        self.prompts = []
        self.fits = fits or {}
        self.reply = reply

    def __call__(self, prompt):
        self.calls += 1
        self.prompts.append(prompt)
        if self.reply is not None:
            return self.reply(prompt) if callable(self.reply) else self.reply
        return json.dumps([{"tmdb_id": int(i), "fit": self.fits.get(int(i), 5.0),
                            "why": "契合"} for i in ID_RE.findall(prompt)])


def cand(tmdb_id, score, title="T"):
    return {"tmdb_id": tmdb_id, "score": score, "title": title, "year": 2000,
            "overview": "ov"}


# ---------- profile 隐私 ----------

def seed_private_lib(lib):
    """片库：/Volumes 路径 + 观看记录 + 导演 credits。"""
    m1 = lib.upsert_movie(path="/Volumes/Movies/Stalker.mkv", folder="/Volumes/Movies",
                          video_name="Stalker", ext="mkv", title="Stalker", year=1979,
                          size=0, mtime=0.0)
    m2 = lib.upsert_movie(path="/Volumes/Movies/Solaris.mkv", folder="/Volumes/Movies",
                          video_name="Solaris", ext="mkv", title="Solaris", year=1972,
                          size=0, mtime=0.0)
    lib.update_movie_metadata(m1, {"tmdb_id": 101, "status": "matched"})
    lib.update_movie_metadata(m2, {"tmdb_id": 102, "status": "matched"})
    lib.save_playback("movie", m1, 3600.0, 4800.0, True)   # watched + position
    lib.save_credits("movie", m1, [{"id": 70, "name": "Andrei Tarkovsky",
                                    "role": "director", "ord": 0}])
    lib.save_credits("movie", m2, [{"id": 70, "name": "Andrei Tarkovsky",
                                    "role": "director", "ord": 0}])
    return m1, m2


def test_profile_only_aggregates(lib):
    seed_private_lib(lib)
    profile = taste_profile(lib)
    assert profile["directors"] == [{"name": "Andrei Tarkovsky", "films": 2}]
    assert {"decade": "1970s", "films": 2} in profile["eras"]
    blob = json.dumps(profile, ensure_ascii=False)
    for banned in ("/Volumes", "Stalker.mkv", "position", "watched", "path"):
        assert banned not in blob


def test_profile_directors_capped_at_15(lib):
    for i in range(16):
        mid = lib.upsert_movie(path="/Volumes/Movies/m%02d.mkv" % i, folder="/Volumes/Movies",
                               video_name="m", ext="mkv", title="M%02d" % i, year=2000,
                               size=0, mtime=0.0)
        lib.save_credits("movie", mid, [{"id": 500 + i, "name": "Director %02d" % i,
                                         "role": "director", "ord": 0}])
    profile = taste_profile(lib)
    assert len(profile["directors"]) == 15


def test_prompt_carries_only_profile_and_public_candidate_fields(lib):
    seed_private_lib(lib)
    profile = taste_profile(lib)
    long_overview = "x" * 201
    cands = [dict(cand(900, 3.0, "Some Film"), original_title="Original",
                  overview=long_overview)]
    prompt = build_prompt(cands, profile)
    assert "Andrei Tarkovsky" in prompt          # profile 进 prompt
    assert "1970s" in prompt
    assert "x" * 200 in prompt and long_overview not in prompt  # 简介截 200 字
    for banned in ("/Volumes", "position", "watched", "Stalker.mkv"):
        assert banned not in prompt


# ---------- 批次切分 ----------

def test_batches_split_by_20():
    cands = [cand(i, 50.0 - i) for i in range(1, 46)]
    llm = FakeLLM()
    out = rerank_movies(cands, {}, llm)
    assert llm.calls == 3
    assert [len(ID_RE.findall(p)) for p in llm.prompts] == [20, 20, 5]
    assert len(out) == 45


def test_batch_size_override():
    cands = [cand(i, 10.0) for i in range(25)]
    llm = FakeLLM()
    rerank_movies(cands, {}, llm, batch=10)
    assert [len(ID_RE.findall(p)) for p in llm.prompts] == [10, 10, 5]


# ---------- 解析失败 / 调用失败 ----------

def test_garbage_batch_keeps_order_and_warns(caplog):
    cands = [cand(i, 40.0 - i) for i in range(1, 5)]  # 分数降序输入
    llm = FakeLLM(reply="I cannot output JSON sorry")
    with caplog.at_level(logging.WARNING, logger="homecinema.rerank"):
        out = rerank_movies(cands, {}, llm, batch=2)
    assert [c["tmdb_id"] for c in out] == [1, 2, 3, 4]
    assert all(c["fit"] is None for c in out)
    assert any("保留原顺序" in r.getMessage() for r in caplog.records)


def test_mixed_batches_failed_one_keeps_its_order():
    cands = [cand(i, 40.0 - i) for i in range(1, 5)]

    def reply(prompt):
        ids = [int(i) for i in ID_RE.findall(prompt)]
        if 1 in ids:
            return "{{{ not json"
        return json.dumps([{"tmdb_id": i, "fit": 10, "why": "好"} for i in ids])

    out = rerank_movies(cands, {}, FakeLLM(reply=reply), batch=2)
    order = [c["tmdb_id"] for c in out]
    # 失败批 fit 记 0 → 沉底但保持相互顺序；成功批 fit=10 排前
    assert order == [3, 4, 1, 2]


def test_llm_exception_does_not_raise():
    def boom(_prompt):
        raise RuntimeError("network down")
    cands = [cand(i, 40.0 - i) for i in range(1, 4)]
    out = rerank_movies(cands, {}, boom, batch=2)
    assert [c["tmdb_id"] for c in out] == [1, 2, 3]


# ---------- 排序公式 ----------

def test_final_score_formula():
    cands = [cand(1, 3.0), cand(2, 1.5)]
    out = rerank_movies(cands, {}, FakeLLM(fits={1: 4.0, 2: 10.0}))
    expect = {1: SCORE_WEIGHT * 1.0 + FIT_WEIGHT * 0.4,
              2: SCORE_WEIGHT * 0.5 + FIT_WEIGHT * 1.0}
    by_id = {c["tmdb_id"]: c for c in out}
    for cid, score in expect.items():
        assert by_id[cid]["final_score"] == pytest.approx(score)
    assert out[0]["tmdb_id"] == 2      # fit 能翻盘：0.8 > 0.64
    assert by_id[1]["fit"] == 4.0 and by_id[1]["why"] == "契合"


# ---------- generate 集成 ----------

def seed_sources_and_recs(lib):
    add_movie(lib, "Alpha", tmdb_id=501, watched=True)   # 权重 3
    add_movie(lib, "Beta", tmdb_id=502)                  # 权重 1
    add_show(lib, "Show Source", tmdb_id=601, watched_episodes=3)
    client, http = make_tmdb()
    http.add("/movie/501/recommendations", [movie_cand(900, "Popular Hit"),
                                            movie_cand(901, "Slow Cinema")])
    http.add("/movie/502/recommendations", [movie_cand(901, "Slow Cinema")])
    http.add("/tv/601/recommendations", [tv_cand(950, "Show Rec")])
    http.add("image.tmdb.org", FakeResponse(200, content=b"img"))
    return client


def test_generate_rerank_stores_fit_appends_why(cfg, lib):
    client = seed_sources_and_recs(lib)
    llm = FakeLLM(fits={900: 1.0, 901: 10.0})
    generate(lib, client, cfg.cache_dir_path, cfg.images_dir,
             rerank=True, llm_call=llm)
    rows = {r["tmdb_id"]: r for r in lib.list_recommendations()}
    # 原分数：900=3.0，901=3×(1-1/40)+1=3.925；归一化后 900≈0.764
    exp900 = SCORE_WEIGHT * (3.0 / 3.925) + FIT_WEIGHT * 0.1
    exp901 = SCORE_WEIGHT * 1.0 + FIT_WEIGHT * 1.0
    assert rows[900]["score"] == pytest.approx(exp900)
    assert rows[901]["score"] == pytest.approx(exp901)
    assert rows[900]["fit"] == 1.0 and rows[901]["fit"] == 10.0
    assert json.loads(rows[900]["because"])[-1] == "契合"      # why 追加在后
    assert json.loads(rows[900]["because"])[:-1] == ["Alpha"]  # 原理由保留
    # 剧集不受影响
    assert rows[950]["fit"] is None
    assert rows[950]["score"] == pytest.approx(3.0)
    assert json.loads(rows[950]["because"]) == ["Show Source"]


def test_generate_rerank_off_identical_to_legacy(cfg, lib):
    """关闭 rerank：不调用 LLM，分数/理由/顺序与旧行为完全一致（对照 test_recommend 期望值）。"""
    client = seed_sources_and_recs(lib)
    llm = FakeLLM()
    generate(lib, client, cfg.cache_dir_path, cfg.images_dir, llm_call=llm)
    assert llm.calls == 0
    rows = lib.list_recommendations()
    by_id = {r["tmdb_id"]: r for r in rows}
    assert by_id[900]["score"] == pytest.approx(3.0)
    assert by_id[901]["score"] == pytest.approx(3 * (1 - 1 / 40) + 1 * (1 - 0 / 40))
    assert [r["tmdb_id"] for r in rows if r["kind"] == "movie"] == [901, 900]
    assert by_id[901]["fit"] is None
    assert json.loads(by_id[900]["because"]) == ["Alpha"]


def test_generate_default_rerank_is_false():
    assert inspect.signature(generate).parameters["rerank"].default is False


# ---------- CLI 与迁移 ----------

def test_cli_recommend_has_rerank_flag():
    project_root = Path(__file__).resolve().parents[1]
    out = subprocess.run([sys.executable, "-m", "homecinema", "recommend", "--help"],
                         cwd=str(project_root), capture_output=True, text=True, timeout=60)
    assert "--rerank" in out.stdout


def test_old_db_upgrades_with_fit_column(tmp_path):
    db_file = tmp_path / "old.db"
    lib = Library(db_file)
    lib.close()
    raw = sqlite3.connect(db_file)
    raw.execute("ALTER TABLE recommendations DROP COLUMN fit")
    raw.commit()
    assert "fit" not in {r[1] for r in raw.execute("PRAGMA table_info(recommendations)")}
    raw.close()

    lib2 = Library(db_file)  # 迁移：重开自动补列
    try:
        lib2.save_recommendations([
            {"kind": "movie", "tmdb_id": 900, "title": "X", "year": 2000,
             "overview": "", "score": 0.5, "because": "[]", "fit": 7.0,
             "generated_at": "2026-01-01T00:00:00+00:00"},
            {"kind": "tv", "tmdb_id": 950, "title": "Y", "year": 2001,
             "overview": "", "score": 2.0, "because": "[]",
             "generated_at": "2026-01-01T00:00:00+00:00"},
        ])
        rows = {r["tmdb_id"]: r for r in lib2.list_recommendations()}
        assert rows[900]["fit"] == 7.0
        assert rows[950]["fit"] is None   # 没传 fit 的旧式调用照常可写
    finally:
        lib2.close()
