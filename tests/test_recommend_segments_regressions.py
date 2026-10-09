"""回归：2026-10-03 ZCode GLM-5.3-Flash 审计 recommend.py/segments.py 找到的 2 个低严重度 bug（修复前失败）。

每条发现一个 test_FXX_* 函数，只断言「正确行为」，
因此在当前代码上应当失败（失败输出即复现证据，进 AUDIT.md）。
运行命令（仅此一条）：
  .venv/bin/python -m pytest -q tests/test_audit_recommend_segments.py
"""

from __future__ import annotations

import json

from homecinema import recommend, segments


# ---------- F01 ----------

def test_F01_read_cache_missing_or_bad_fetched_at_must_be_stale(tmp_path):
    """recommend._read_cache：缓存条目缺 fetched_at（或 fetched_at 无法解析）时，
    无法证明它在 7 天内写入，应按过期处理返回 None；
    当前代码经 _older_than_days(None) -> False 把它当成永久新鲜（TTL fail-open）。"""
    path = tmp_path / "recs" / "movie_501.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"results": [{"id": 900}]}), encoding="utf-8")
    assert recommend._read_cache(path) is None
    path.write_text(
        json.dumps({"fetched_at": "not-a-timestamp", "results": [{"id": 900}]}),
        encoding="utf-8")
    assert recommend._read_cache(path) is None


# ---------- F02 ----------

def test_F02_vote_tie_between_disjoint_clusters_prefers_earliest_start():
    """两簇候选票数持平（各 2 票、簇间不重叠）时，detect_season 文档写明
    「并列取起点最早的」；当前实现对两个不相交簇统一取中位数再挑最近，
    选出一个任意成员。真实场景：某集与不同邻居分别对上两段不同音频
    （如真正的片头 vs 片尾彩蛋），两段候选票数持平。"""
    picked = segments._vote([(50.0, 70.0), (52.0, 72.0), (10.0, 30.0), (12.0, 32.0)])
    assert picked == (10.0, 30.0)
