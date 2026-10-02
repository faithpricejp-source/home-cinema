"""剧集片头/片尾批量识别：章节优先，其余集用声纹。命令行入口见 __main__.cmd_detect_segments。

对外只依赖 homecinema/segments.py 的四个名字（fingerprint / find_shared / detect_season /
chapters_to_segments）。这里假定：
  - chapters_to_segments(chapters, duration) -> {"intro": (s,e)|None, "credits": (s,e)|None}
  - detect_season(fps, offset_sec=..) -> {key: (s,e)|None}，key 与传入 fps 的键一致，
    秒数相对 offset_sec。
真实实现由另一执行方替换 segments.py；形状对不上会落进「没把握」并在测试里用假实现钉住。

ffprobe 调用包在 probe_chapters / probe_duration 两个函数里，测试里 monkeypatch 它们。
"""

from __future__ import annotations

import json
import subprocess

from . import segments
from .db import Library

INTRO_MAX_SEC = 600.0        # 片头指纹最长取 600 秒
INTRO_RATIO = 0.35           # 或时长的 35%，取小者
CREDITS_LEN_SEC = 300.0      # 片尾指纹取最后 300 秒


def probe_chapters(path: str) -> list[dict]:
    """ffprobe -v error -show_chapters -of json <path>：返回章节列表。"""
    data = _run_ffprobe(["-show_chapters", path])
    chapters = data.get("chapters")
    return chapters if isinstance(chapters, list) else []


def probe_duration(path: str) -> float | None:
    """ffprobe -v error -show_entries format=duration -of json <path>：返回秒数。"""
    data = _run_ffprobe(["-show_entries", "format=duration", path])
    try:
        return float(data["format"]["duration"])
    except (KeyError, TypeError, ValueError):
        return None


def _run_ffprobe(extra: list[str]) -> dict:
    cmd = ["ffprobe", "-v", "error", *extra, "-of", "json"]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return json.loads(result.stdout or "{}")


def run_detect_segments(db: Library, show_id: int | None = None,
                        limit_seasons: int | None = None, progress=print) -> int:
    """对每个需要检测的季跑一遍；每季做完立即落库（中断后重跑会跳过已完成的季）。
    返回本次处理的季数。"""
    seasons = db.seasons_needing_segments(show_id)
    if limit_seasons is not None:
        seasons = seasons[:max(0, limit_seasons)]
    total = len(seasons)
    for index, (sid, season_number, episodes) in enumerate(seasons, start=1):
        show = db.get_show(sid)
        title = show["title"] if show is not None else str(sid)
        detect_season(db, season_number, episodes, show_title=title,
                      index=index, total=total, progress=progress)
    return total


def detect_season(db: Library, season_number: int, episodes: list, *,
                  show_title: str, index: int, total: int, progress=print) -> None:
    """处理一整季，逐集落库。单集出错按 none 计，不中断。"""
    durations: dict[int, float | None] = {
        ep["id"]: _safe_duration(ep["path"]) for ep in episodes}

    results: dict[int, dict] = {}
    pending: list = []  # 章节没命中的集，留给声纹
    for ep in episodes:
        intro = credits = None
        try:
            chapters = probe_chapters(ep["path"])
            seg = segments.chapters_to_segments(chapters, durations[ep["id"]] or 0.0) or {}
            intro = _as_range(seg.get("intro"))
            credits = _as_range(seg.get("credits"))
        except Exception:
            intro = credits = None
        results[ep["id"]] = {"intro": intro, "credits": credits,
                             "source": "chapters" if (intro or credits) else "none"}
        # 章节只给了片头或只给了片尾的集（常见：只标了 Credits），缺的那段仍要用声纹补；
        # 章节两段都有的集也参与声纹比对——它是同季其他集的比对对象，结果不会覆盖章节值
        pending.append(ep)

    # 一季只有 1 集跳过声纹
    if len(episodes) > 1 and pending:
        _fingerprint_pass(results, pending, durations)

    for ep in episodes:
        item = results[ep["id"]]
        db.save_segments(ep["id"], item["intro"], item["credits"], item["source"])

    intro_hits = sum(1 for ep in episodes if results[ep["id"]]["intro"])
    credit_hits = sum(1 for ep in episodes if results[ep["id"]]["credits"])
    progress(f"[{index}/{total}] {show_title} S{season_number:02d}："
             f"片头 {intro_hits}/{len(episodes)} 集，片尾 {credit_hits}/{len(episodes)} 集",
             flush=True)


def _fingerprint_pass(results: dict, pending: list, durations: dict) -> None:
    """对章节没命中的集做声纹比对：片头一次、片尾一次。"""
    by_path = {ep["path"]: ep for ep in pending}

    # 片头：每集取 [0, L)，L = min(600, 时长×0.35)，整季一起 detect_season(offset=0)
    intro_fps: dict[str, list[int]] = {}
    for ep in pending:
        dur = durations.get(ep["id"])
        if not dur or dur <= 0:
            continue
        length = min(INTRO_MAX_SEC, dur * INTRO_RATIO)
        try:
            intro_fps[ep["path"]] = segments.fingerprint(ep["path"], 0.0, length)
        except Exception:
            pass  # 单集声纹失败按 none 继续
    for path, rng in _detect(intro_fps).items():
        rng = _as_range(rng)
        if rng is not None and path in by_path and results[by_path[path]["id"]]["intro"] is None:
            results[by_path[path]["id"]]["intro"] = rng
            results[by_path[path]["id"]]["_fp"] = True

    # 片尾：每集取 [D-300, D)，各集 offset 不同，detect_season 用 offset=0，
    # 拿到相对秒数后各自加回 (D - 300)
    credit_fps: dict[str, list[int]] = {}
    for ep in pending:
        dur = durations.get(ep["id"])
        if not dur or dur < CREDITS_LEN_SEC:
            continue
        try:
            credit_fps[ep["path"]] = segments.fingerprint(
                ep["path"], dur - CREDITS_LEN_SEC, CREDITS_LEN_SEC)
        except Exception:
            pass
    for path, rng in _detect(credit_fps).items():
        rel = _as_range(rng)
        ep = by_path.get(path)
        if rel is None or ep is None:
            continue
        if results[ep["id"]]["credits"] is not None:
            continue  # 章节给过的不覆盖
        base = (durations.get(ep["id"]) or 0.0) - CREDITS_LEN_SEC
        results[ep["id"]]["credits"] = (rel[0] + base, rel[1] + base)
        results[ep["id"]]["_fp"] = True

    for ep in pending:
        item = results[ep["id"]]
        if item.pop("_fp", False):
            item["source"] = "chapters+fingerprint" if item["source"] == "chapters" else "fingerprint"


def _detect(fps: dict[str, list[int]]) -> dict:
    """调 detect_season；空输入或异常都返回空 dict（整段按 none 继续）。"""
    if not fps:
        return {}
    try:
        out = segments.detect_season(fps, offset_sec=0.0)
    except Exception:
        return {}
    return out if isinstance(out, dict) else {}


def _safe_duration(path: str) -> float | None:
    try:
        return probe_duration(path)
    except Exception:
        return None


def _as_range(value):
    """把 (start, end) / {"start","end"} 归一成 (float, float)；否则 None。"""
    if value is None:
        return None
    if isinstance(value, dict):
        start = value.get("start", value.get("intro_start", value.get("credits_start")))
        end = value.get("end", value.get("intro_end", value.get("credits_end")))
        if start is None or end is None:
            return None
        return (float(start), float(end))
    try:
        start, end = value[0], value[1]
    except (TypeError, IndexError, KeyError):
        return None
    return (float(start), float(end))
