"""按口味重排：电影推荐的可选第二步（只作用于电影，不动剧集）。

`taste_profile` 只从片库聚合统计量（导演部数、片龄分布）——不含观看记录、
文件路径与任何个人信息，因为这些内容会连同 prompt 发给外部 LLM。
`rerank_movies` 把候选按批交给 `llm_call(prompt) -> str` 打契合分 fit（0-10），
最终分 = 原分数归一化 × SCORE_WEIGHT + fit/10 × FIT_WEIGHT；
解析失败的批次保留原顺序并记 warning，不抛异常。
"""

from __future__ import annotations

import json
import logging
import re
import os
import sys

from .db import _movie_group_key

SCORE_WEIGHT = 0.4           # 原分数（按候选集最大值归一化）的权重
FIT_WEIGHT = 0.6             # LLM 口味契合分（fit/10）的权重
BATCH_DEFAULT = 20           # 每次 LLM 调用最多带的候选条数
OVERVIEW_CHARS = 200         # 进 prompt 的 TMDB 简介截断长度
TOP_DIRECTORS = 15           # 口味画像保留的导演条数
WHY_MAX_CHARS = 40           # 理由进推荐理由的最大长度

FREE_LLM_DIR = os.environ.get("HOMECINEMA_FREE_LLM_DIR", "")  # 可选：提供 free_llm.py 的目录；不设则跳过 LLM 重排
REASONING_EFFORT = "medium"                   # 判定类任务不用 low；high 在长 prompt 上常超时
NEUTRAL_FIT = 5.0           # 没拿到 fit 的条目按中性分算，不因 LLM 失败沉底

log = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"```(?:json)?|```")


# ---------- 口味画像 ----------

def taste_profile(db) -> dict:
    """片库聚合统计：导演 top15（名字+部数）、年代分布（每个年代计数）。

    制片国家/原始语言：movies/shows 表没有这两个字段，credits/people 也推不出来，
    按约定省略（见 CHANGES.md）。只读不写，全程不碰 playback 表。
    """
    directors = [{"name": p["name"], "films": int(p["count"])}
                 for p in db.people_by_role(("director",), collapse_movies=True)[:TOP_DIRECTORS]]
    movies = db.list_movies()  # 只含在架（missing=0），不带 playback 列之外的隐私字段
    # 同片多版本折叠成一部：画像的语义是「几部片」，不是「几个文件」
    films: dict[tuple, object] = {}
    for row in movies:
        films.setdefault(_movie_group_key(row), row)
    eras: dict[int, int] = {}
    for row in films.values():
        year = row["year"]
        if year:
            decade = int(year) // 10 * 10
            eras[decade] = eras.get(decade, 0) + 1
    return {
        "total_movies": len(films),
        "directors": directors,
        "eras": [{"decade": f"{d}s", "films": n} for d, n in sorted(eras.items())],
    }


# ---------- prompt ----------

def candidate_line(cand: dict) -> str:
    """单条候选 → prompt 里的一行 JSON。导演有就带、没有就整个字段省略。"""
    line = {"tmdb_id": int(cand["tmdb_id"]),
            "title": cand.get("title"),
            "original_title": cand.get("original_title"),
            "year": cand.get("year"),
            "overview": (cand.get("overview") or "")[:OVERVIEW_CHARS]}
    if cand.get("directors"):
        line["directors"] = list(cand["directors"])
    return json.dumps(line, ensure_ascii=False)


def build_prompt(batch: list[dict], profile: dict) -> str:
    lines = "\n".join(candidate_line(c) for c in batch)
    return (
        "你是私人片库的选片顾问。以下是我的片库口味画像，只有聚合统计（导演部数、"
        "每个年代的电影数量），没有任何观看记录：\n"
        + json.dumps(profile, ensure_ascii=False)
        + "\n\n请为稍后列出的每部候选电影打一个口味契合分 fit（0-10 的整数）：\n"
        "- 10：与画像明显同源——命中我收藏最多的作者导演、收藏年代或全集式系列口味\n"
        "- 5：一般，没有特别的契合点也没有冲突\n"
        "- 0：与画像气质相反，例如与作者/年代收藏无关的流量爆款\n"
        "注意：不要因为它是大众热门或评分高就给高分，宁可挑剔。\n\n"
        "候选电影（每行一个 JSON）：\n" + lines + "\n\n"
        "只输出一个 JSON 数组，不要输出任何其他文字。每个元素形如：\n"
        '{"tmdb_id": <整数，与候选一致>, "fit": <0-10>, "why": "<不超过40字的中文依据>"}\n'
        "必须覆盖每一条候选的 tmdb_id。\n\n"
        "why 的硬约束（违反视为无效）：\n"
        "- why 只能引用该候选行里给出的 directors、year、标题信息和画像里的统计，"
        "不得引用其他来源的信息\n"
        "- 画像里的导演名只有当它同时出现在该候选行的 directors 列表里时，"
        "才可以写「命中」或提及该导演\n"
        "- 候选行没有 directors 字段时，why 里不许出现任何导演名"
    )


# ---------- 解析 ----------

def _json_array(text: str) -> list | None:
    cleaned = _FENCE_RE.sub("", str(text)).strip()
    try:
        data = json.loads(cleaned)
    except ValueError:
        start, end = cleaned.find("["), cleaned.rfind("]")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(cleaned[start:end + 1])
        except ValueError:
            return None
    return data if isinstance(data, list) else None


def parse_batch_reply(reply: str, ids: set[int]) -> dict[int, tuple[float, str]] | None:
    """返回 {tmdb_id: (fit, why)}；整批解析不出来返回 None（调用方保留原顺序）。

    单条缺失/字段非法只丢那一条（按无 fit 计），不算整批失败。
    """
    data = _json_array(reply)
    if data is None:
        return None
    out: dict[int, tuple[float, str]] = {}
    for entry in data:
        if not isinstance(entry, dict):
            continue
        cid = entry.get("tmdb_id")
        fit = entry.get("fit")
        if not isinstance(cid, int) or isinstance(cid, bool) or cid not in ids:
            continue
        if isinstance(fit, bool) or not isinstance(fit, (int, float)):
            continue
        why = str(entry.get("why") or "").strip()[:WHY_MAX_CHARS]
        out[cid] = (max(0.0, min(10.0, float(fit))), why)
    return out or None


# ---------- 重排 ----------

def _why_is_grounded(why: str, cand: dict, profile: dict) -> bool:
    """机械校验：why 引用了画像导演名但该名字不在候选 directors 里 → 视为编造。

    候选没有 directors 时，why 出现任何画像导演名都算编造。
    """
    cand_directors = set(cand.get("directors") or [])
    for entry in profile.get("directors") or []:
        name = entry.get("name") if isinstance(entry, dict) else None
        if not name or name in cand_directors:
            continue
        # 中文译名常被简称（「克里斯托弗·诺兰」→「诺兰」），也查 · 或空格后的最后一段（≥2 字）
        forms = {name}
        for sep in ("·", "・", " "):
            if sep in name:
                tail = name.rsplit(sep, 1)[-1].strip()
                if len(tail) >= 2:
                    forms.add(tail)
        if any(f in why for f in forms):
            return False
    return True


def rerank_movies(candidates: list[dict], profile: dict, llm_call,
                  batch: int = BATCH_DEFAULT) -> list[dict]:
    """电影候选二段重排；输入是 dict 列表（需含 tmdb_id 与 score），返回新列表。

    每项附加 fit（0-10 float 或 None）、why（str 或 None）与 final_score，
    按 final_score = SCORE_WEIGHT×(score/max_score) + FIT_WEIGHT×fit/10 降序。
    LLM 调用失败或整批解析失败的批次：该批按无 fit 计（fit 记 None、不丢条目），
    批次内部保持输入顺序（输入本身按原分数降序 + 稳定排序），只记 warning。
    """
    items = [dict(c) for c in candidates]
    fits: dict[int, tuple[float, str]] = {}
    step = max(1, int(batch))
    for start in range(0, len(items), step):
        part = items[start:start + step]
        ids = {int(c["tmdb_id"]) for c in part}
        try:
            reply = llm_call(build_prompt(part, profile))
        except Exception as exc:
            log.warning("rerank：第 %d 批 LLM 调用失败（%s），保留原顺序", start, exc)
            continue
        parsed = parse_batch_reply(reply, ids)
        if parsed is None:
            log.warning("rerank：第 %d 批回复解析失败，保留原顺序", start)
            continue
        fits.update(parsed)

    top = max((float(c.get("score") or 0.0) for c in items), default=0.0)
    for idx, c in enumerate(items):
        c["order"] = idx
        norm = (float(c.get("score") or 0.0) / top) if top > 0 else 0.0
        fit, why = fits.get(int(c["tmdb_id"]), (None, ""))
        if why and not _why_is_grounded(why, c, profile):
            log.debug("rerank：候选 %s 的 why 引用了不属于它的画像导演名，置空：%s",
                      c["tmdb_id"], why)
            why = ""
        c["fit"] = fit
        c["why"] = why or None
        c["final_score"] = SCORE_WEIGHT * norm + FIT_WEIGHT * ((NEUTRAL_FIT if fit is None else fit) / 10.0)
    items.sort(key=lambda c: (-c["final_score"], c["order"]))
    for c in items:
        c.pop("order", None)
    return items


# ---------- 默认 LLM 通道 ----------

def default_llm_call(prompt: str) -> str:
    """free_llm.chat 的薄封装（sys.path.insert 引入，与 config 里其他工具路径同风格）。

    判定类任务不用 reasoning effort=low：先按签名探测常见的强度参数名，
    支持就传 high，不支持就用 chat 的默认值（见 CHANGES.md）。
    测试一律注入假 llm_call，不会走到这里，也就不会联网。
    """
    if FREE_LLM_DIR and FREE_LLM_DIR not in sys.path:
        sys.path.insert(0, FREE_LLM_DIR)
    from free_llm import chat
    # chat 返回 {text, backend, model}；推理强度走 extra_body（库默认 low）
    out = chat(prompt, max_tokens=3000, task="home-cinema rerank",
               extra_body={"reasoning": {"effort": REASONING_EFFORT}})
    return out["text"] if isinstance(out, dict) else str(out)
