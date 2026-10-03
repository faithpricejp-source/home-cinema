"""把文字识别（及第二层视觉模型）的片尾起点合并进 segments 表。

规则（播放端到片尾起点就直接跳下一集，所以宁晚勿早）：
  - 原来没有片尾：用文字识别结果；文字识别也没有，用视觉模型结果（vlm_results.jsonl）。
  - 文字识别认不出、原值是纯声纹：视觉模型比原值晚 10 秒以上才改（只往后挪）。
  - 原来有片尾、文字识别也有，两者差 10 秒以上：用文字识别的。
    抽查过的分歧里，声纹偏早（切掉结尾剧情）和偏晚（只认出片尾后半段的配乐）都是声纹错。
    往前挪只对纯声纹来源做；原值含章节标注时只允许往后挪。
  - 差 10 秒以内：保留原值（章节标注最准）。
片头不动。source 追加 +ocr / +vlm。默认只打印统计，--write 才落库。
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from homecinema import ocr_credits as oc  # noqa: E402
from homecinema.db import Library  # noqa: E402

DB = os.path.expanduser("~/Library/Application Support/HomeCinema/library.db")
CACHE = os.path.expanduser("~/Library/Caches/HomeCinema/ocr_tail")
VLM = os.path.expanduser("~/Library/Caches/HomeCinema/vlm_results.jsonl")
AGREE_SEC = 10.0


def load_vlm() -> dict[int, float]:
    out = {}
    if os.path.exists(VLM):
        for line in open(VLM, encoding="utf-8"):
            rec = json.loads(line)
            if rec.get("credits") is not None:
                out[int(rec["eid"])] = float(rec["credits"])
    return out


def plan() -> list[tuple]:
    con = sqlite3.connect(DB)
    rows = {r[0]: r[1:] for r in con.execute(
        "SELECT e.id, s.intro_start, s.intro_end, s.credits_start, s.credits_end, s.source "
        "FROM episodes e JOIN segments s ON s.episode_id = e.id")}
    vlm = load_vlm()
    changes = []
    for f in glob.glob(os.path.join(CACHE, "*.json")):
        eid = int(os.path.basename(f)[:-5])
        if eid not in rows:
            continue
        i0, i1, c0, c1, source = rows[eid]
        if "+ocr" in source or "+vlm" in source:
            continue  # 已合并过
        data = json.load(open(f, encoding="utf-8"))
        ocr = oc.decide(data)
        dur = data["duration"]
        if ocr is not None and (c0 is None or abs(ocr - c0) > AGREE_SEC):
            kind = "fill" if c0 is None else ("later" if ocr > c0 else "earlier")
            if kind == "earlier" and source != "fingerprint":
                continue  # 章节标注是人标的，文字识别更早时多半是演职员表叠在收尾剧情上，不往前挪
            changes.append((eid, (i0, i1), c0, ocr, dur, source + "+ocr", kind))
        elif ocr is None and eid in vlm:
            if c0 is None:
                changes.append((eid, (i0, i1), None, vlm[eid], dur, source + "+vlm", "vlm-fill"))
            elif source == "fingerprint" and vlm[eid] > c0 + AGREE_SEC:
                # 视觉模型只用来把声纹的偏早值往后挪，不往前挪
                changes.append((eid, (i0, i1), c0, vlm[eid], dur, source + "+vlm", "vlm-later"))
    return changes


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()
    changes = plan()
    from collections import Counter
    print("变更统计：", dict(Counter(c[6] for c in changes)))
    if not args.write:
        return 0
    db = Library(DB)
    for eid, intro, _old, new, dur, source, _kind in changes:
        intro_rng = intro if intro[0] is not None else None
        db.save_segments(eid, intro_rng, (round(new, 2), round(dur, 2)), source)
    print(f"已写入 {len(changes)} 集")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
