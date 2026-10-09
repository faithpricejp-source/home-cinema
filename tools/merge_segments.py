"""把文字识别、视觉模型、社区库三路结果合并进 segments 表。

片尾（播放端到片尾起点就直接跳下一集，所以宁晚勿早），按优先级取第一个有结果的来源：
  1. 文字识别（ocr_tail 缓存）：原来没有片尾就补；与原值差 10 秒以上改用文字识别值。
     抽查过的分歧里，声纹偏早（切掉结尾剧情）和偏晚（只认出片尾后半段的配乐）都是声纹错。
     往前挪只对纯声纹来源做；原值含章节标注时只允许往后挪。
  2. 视觉模型（vlm_results.jsonl）：补空；原值是纯声纹时只往后挪。
  3. 社区库（community_segments）：同 2。
  差 10 秒以内保留原值（章节标注最准）。
片头：原来没有片头才用社区库的（长 5–200 秒、结束在前 40% 以内）；有就不动。
source 追加 +ocr / +vlm / +cdb（片尾）和 +cdb-intro（片头）；已带标记的那一项不再改。
默认只打印统计，--write 才落库。
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from homecinema import ocr_credits as oc  # noqa: E402
from homecinema.db import Library  # noqa: E402

DB = os.path.expanduser("~/Library/Application Support/HomeCinema/library.db")
CACHE = os.path.expanduser("~/Library/Caches/HomeCinema/ocr_tail")
VLM = os.path.expanduser("~/Library/Caches/HomeCinema/vlm_results.jsonl")
AGREE_SEC = 10.0
INTRO_MIN, INTRO_MAX, INTRO_END_RATIO = 5.0, 200.0, 0.4


def load_vlm() -> dict[int, float]:
    out = {}
    if os.path.exists(VLM):
        for line in open(VLM, encoding="utf-8"):
            if not line.strip():
                continue
            try:
                # Kimi-P7: 残行/空行跳过（写入方 append 非原子，被杀留残行），
                # 坏一行不能让 plan() 崩溃卡死整个合并
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("credits") is not None:
                out[int(rec["eid"])] = float(rec["credits"])
    return out


def _credits_change(c0, source, new, tag, allow_earlier):
    """返回 (新片尾起点, 标记, 类别) 或 None。"""
    if new is None or (c0 is not None and abs(new - c0) <= AGREE_SEC):
        return None
    if c0 is None:
        return new, tag, f"{tag}-fill"
    if new > c0:
        # 文字识别往后挪不限来源；视觉模型/社区库只挪纯声纹的
        return (new, tag, f"{tag}-later") if tag == "ocr" or source == "fingerprint" else None
    if allow_earlier and source == "fingerprint":
        return new, tag, f"{tag}-earlier"
    return None


def plan() -> list[dict]:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT e.id, e.path, e.runtime_minutes, s.intro_start, s.intro_end, s.credits_start, s.credits_end, s.source, "
        "c.status AS c_status, c.intro_start AS c_intro_start, c.intro_end AS c_intro_end, c.credits_start AS c_credits "
        "FROM episodes e JOIN segments s ON s.episode_id = e.id "
        "LEFT JOIN community_segments c ON c.episode_id = e.id WHERE e.missing = 0").fetchall()
    vlm = load_vlm()
    changes = []
    for r in rows:
        eid, source = r["id"], r["source"]
        intro = (r["intro_start"], r["intro_end"]) if r["intro_start"] is not None else None
        c0 = r["credits_start"]
        cache = os.path.join(CACHE, f"{eid}.json")
        data = json.load(open(cache, encoding="utf-8")) if os.path.exists(cache) else None
        if data is not None:
            try:
                st = os.stat(r["path"]) if r["path"] else None
            except OSError:
                st = None
            # fix-1007-T-1: 本工具只合并、不重截帧；指纹不符就跳过，避免旧帧写进片尾
            if not oc.cache_matches(data, st):
                print(f"警告: 集 {eid} OCR 缓存与源文件指纹不符，跳过", file=sys.stderr)
                data = None
        dur = data["duration"] if data else (r["runtime_minutes"] * 60.0 if r["runtime_minutes"] else None)
        new_credits, kinds, tags = None, [], []

        tokens = set(source.split("+"))
        # 每个来源的标记只挡它自己：社区库补过片尾之后，文字识别跑出更准的值仍要能覆盖
        picks = []
        if "ocr" not in tokens:
            ocr = oc.decide(data) if data else None
            if ocr is not None:
                picks.append(("ocr", ocr, True))
        if "vlm" not in tokens and eid in vlm:
            picks.append(("vlm", vlm[eid], False))
        if "cdb" not in tokens and r["c_status"] == "ok" and r["c_credits"] is not None:
            picks.append(("cdb", r["c_credits"], False))
        if picks:
            tag, value, allow_earlier = picks[0]  # 优先级 ocr > vlm > cdb，只取第一个有结果的来源
            pick = _credits_change(c0, source, value, tag, allow_earlier)
            if pick:
                new_credits = pick[0]
                tags.append("+" + pick[1])
                kinds.append(pick[2])

        if intro is None and "cdb-intro" not in tokens and r["c_status"] == "ok" \
                and r["c_intro_end"] is not None and dur:
            s, e = r["c_intro_start"] or 0.0, r["c_intro_end"]
            if INTRO_MIN <= e - s <= INTRO_MAX and e <= dur * INTRO_END_RATIO:
                intro = (s, e)
                tags.append("+cdb-intro")
                kinds.append("cdb-intro-fill")

        if kinds:
            if new_credits is not None:
                # dur 可能是 TMDB 名义时长，比片尾起点还早；兜不住就不写 end，
                # 留给播放端按整集时长处理，绝不写 end < start
                end = max(dur, new_credits) if dur else None
                credits = (round(new_credits, 2), round(end, 2) if end is not None else None)
            else:
                credits = (c0, r["credits_end"]) if c0 is not None else None
            changes.append({"eid": eid, "intro": intro, "credits": credits,
                            "source": source + "".join(tags), "kinds": kinds, "old_credits": c0})
    return changes


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()
    changes = plan()
    print("变更统计：", dict(Counter(k for c in changes for k in c["kinds"])))
    if not args.write:
        return 0
    db = Library(DB)
    for c in changes:
        db.save_segments(c["eid"], c["intro"], c["credits"], c["source"])
    print(f"已写入 {len(changes)} 集")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
