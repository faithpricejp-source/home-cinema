"""把 Wikidata 中文名表导入 people.aka_zh（10-07，供外国影人按中文译名搜索）。

输入 CSV 列：tmdb_person_id,name,qid,zh_labels,zh_aliases（多值用 | 分隔）。
只写有中文名的行；可重跑（整列覆盖该人的 aka_zh）。
用法：.venv/bin/python tools/import_people_zh.py <csv> [--db <library.db>]
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from homecinema.db import Library  # noqa: E402

DEFAULT_DB = Path.home() / "Library/Application Support/HomeCinema/library.db"


def zh_names(row: dict) -> list[str]:
    out: list[str] = []
    for field in ("zh_labels", "zh_aliases"):
        for name in (row.get(field) or "").split("|"):
            name = name.strip()
            if name and name not in out:
                out.append(name)
    return out


def import_csv(db: Library, path: Path) -> dict:
    stats = {"rows": 0, "written": 0, "no_zh": 0, "not_in_db": 0}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            stats["rows"] += 1
            names = zh_names(row)
            if not names:
                stats["no_zh"] += 1
                continue
            if db.set_person_aka_zh(int(row["tmdb_person_id"]), names):
                stats["written"] += 1
            else:
                stats["not_in_db"] += 1
    return stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("csv", type=Path)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    args = ap.parse_args()
    db = Library(args.db)
    try:
        print(import_csv(db, args.csv))
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
