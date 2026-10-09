"""重排导演补全测试：credits 缓存、失败兜底、prompt 约束、编造导演名置空。

一律注入假 TMDB client（FakeHttp）与假 llm_call，不发真实请求。
"""

from __future__ import annotations

import json
import logging

from homecinema.rerank import _why_is_grounded, build_prompt, rerank_movies
from homecinema.recommend import generate
from test_recommend import FakeResponse, add_movie, make_tmdb, movie_cand
from test_rerank import FakeLLM

PROFILE = {"total_movies": 3,
           "directors": [{"name": "Christopher Nolan", "films": 3}],
           "eras": [{"decade": "2010s", "films": 2}]}
CREW = [{"job": "Director", "name": "Atom Egoyan"},
        {"job": "Writer", "name": "Someone Else"},
        {"job": "Director", "name": "Atom Egoyan"}]  # 重复只留一次


def credits_calls(http):
    return [c for c in http.calls if c["url"].endswith("/credits")]


def seed(cfg, lib, credits_responder=FakeResponse(200, {"crew": CREW})):
    add_movie(lib, "Alpha", tmdb_id=501, watched=True)
    client, http = make_tmdb()
    http.add("/movie/501/recommendations", [movie_cand(900, "Remember")])
    http.add("image.tmdb.org", FakeResponse(200, content=b"img"))
    if credits_responder is not None:
        http.add("/movie/900/credits", credits_responder)
    return client, http


# ---------- credits 缓存命中不重复请求 ----------

def test_credits_cached_and_not_refetched(cfg, lib):
    client, http = seed(cfg, lib)
    llm = FakeLLM()
    generate(lib, client, cfg.cache_dir_path, cfg.images_dir,
             rerank=True, llm_call=llm)
    assert len(credits_calls(http)) == 1
    generate(lib, client, cfg.cache_dir_path, cfg.images_dir,
             rerank=True, llm_call=FakeLLM())
    assert len(credits_calls(http)) == 1  # 第二次走 7 天缓存


# ---------- 取不到导演不报错 ----------

def test_credits_http_failure_leaves_directors_empty(cfg, lib):
    client, http = seed(cfg, lib, FakeResponse(404))
    llm = FakeLLM()
    generate(lib, client, cfg.cache_dir_path, cfg.images_dir,
             rerank=True, llm_call=llm)  # 不抛异常
    assert llm.calls == 1
    line = [ln for ln in llm.prompts[0].splitlines() if '"tmdb_id": 900' in ln]
    assert line and "directors" not in line[0]
    rows = {r["tmdb_id"]: r for r in lib.list_recommendations()}
    assert rows[900]["fit"] is not None  # 重排照常完成


def test_credits_route_absent_does_not_raise(cfg, lib):
    """假 http 没注册 credits 路由（抛 AssertionError）也不能中断重排。"""
    client, http = seed(cfg, lib, credits_responder=None)
    generate(lib, client, cfg.cache_dir_path, cfg.images_dir,
             rerank=True, llm_call=FakeLLM())
    assert {r["tmdb_id"] for r in lib.list_recommendations()} == {900}


# ---------- prompt 含候选导演与硬约束 ----------

def test_prompt_carries_candidate_directors(cfg, lib):
    client, http = seed(cfg, lib)
    llm = FakeLLM()
    generate(lib, client, cfg.cache_dir_path, cfg.images_dir,
             rerank=True, llm_call=llm)
    prompt = llm.prompts[0]
    assert "Atom Egoyan" in prompt
    line = [ln for ln in prompt.splitlines() if '"tmdb_id": 900' in ln][0]
    assert json.loads(line)["directors"] == ["Atom Egoyan"]


def test_prompt_has_grounded_why_constraints():
    prompt = build_prompt([{"tmdb_id": 1, "title": "X", "year": 2000,
                            "overview": "", "score": 1.0}], PROFILE)
    assert "只能引用" in prompt
    assert "才可以写「命中」" in prompt
    assert "不许出现任何导演名" in prompt


# ---------- 编造导演名的 why 置空，fit 保留 ----------

def _reply(why):
    return lambda _p: json.dumps([{"tmdb_id": 1, "fit": 9, "why": why}])


def test_fabricated_director_why_cleared_fit_kept(caplog):
    cands = [{"tmdb_id": 1, "score": 3.0, "directors": ["Atom Egoyan"]}]
    with caplog.at_level(logging.DEBUG, logger="homecinema.rerank"):
        out = rerank_movies(cands, PROFILE, _reply("Christopher Nolan 作者命中"))
    assert out[0]["fit"] == 9.0
    assert out[0]["why"] is None
    assert any("导演名" in r.getMessage() for r in caplog.records)


def test_fabricated_director_why_cleared_when_no_directors():
    # 画像导演是英文名、why 用中文简称（如「诺兰」）时机械校验抓不到——由 prompt 约束负责（已知局限）
    cands = [{"tmdb_id": 1, "score": 3.0, "directors": []}]
    out = rerank_movies(cands, PROFILE, _reply("Christopher Nolan 命中"))
    assert out[0]["why"] is None and out[0]["fit"] == 9.0


def test_grounded_director_why_kept():
    cands = [{"tmdb_id": 1, "score": 3.0, "directors": ["Christopher Nolan"]}]
    out = rerank_movies(cands, PROFILE, _reply("Christopher Nolan 作者命中"))
    assert out[0]["why"] == "Christopher Nolan 作者命中"


def test_why_is_grounded_unit():
    assert _why_is_grounded("无关理由", {"directors": []}, PROFILE)
    assert not _why_is_grounded("Christopher Nolan 命中", {}, PROFILE)
    assert _why_is_grounded("Christopher Nolan 命中",
                            {"directors": ["Christopher Nolan"]}, PROFILE)
    assert _why_is_grounded("x", {"directors": []}, {})  # 空画像不拦


def test_why_with_chinese_short_name_is_caught():
    # 验收补：画像里是中文全名，模型常写简称
    from homecinema.rerank import _why_is_grounded
    profile = {"directors": [{"name": "克里斯托弗·诺兰", "films": 9}]}
    assert not _why_is_grounded("诺兰作者命中，悬疑记忆题材", {"directors": ["Atom Egoyan"]}, profile)
    assert _why_is_grounded("诺兰作者命中", {"directors": ["克里斯托弗·诺兰"]}, profile)
