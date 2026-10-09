"""T-1 / T-2：merge_segments 与 vlm_credits 读 OCR 缓存不校验源文件指纹。

直接加载 tools 里的真模块、调用真函数。不起子进程，不发网络请求。
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
from pathlib import Path

from homecinema.db import Library

PROJECT = Path(__file__).resolve().parent.parent


def load_tool(name: str):
    """按文件路径加载 tools/<name>.py（tools 不是包）。"""
    spec = importlib.util.spec_from_file_location(name, PROJECT / "tools" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _credit_frames():
    """真 decide() 能给出片尾起点的帧：先一帧剧情，再 6 帧互不相同的演职员表。"""
    frames = [{"t": 1400.0, "dark": 0.1, "lines": []}]
    for i in range(6):
        frames.append({
            "t": 1412.0 + i * 2,
            "dark": 0.95,
            "lines": [
                [f"Role{i}A", 0.1, 0.6, 0.3, 0.05],
                [f"Name{i}B", 0.1, 0.5, 0.3, 0.05],
            ],
        })
    return frames


def _ocr_payload(file_fp, frames=None):
    payload = {"duration": 1500.0, "start": 1200.0, "step": 2.0,
               "frames": _credit_frames() if frames is None else frames}
    if file_fp is not None:
        payload["file"] = file_fp
    return payload


def _video(tmp_path, name, data=b"x" * 32, mtime=1_700_000_000):
    path = tmp_path / name
    path.write_bytes(data)
    os.utime(path, (mtime, mtime))
    return path


def test_t1_plan_skips_ocr_cache_fingerprint_mismatch(tmp_path, monkeypatch, capsys):
    """换源后 OCR 缓存指纹对不上：plan() 不得用旧帧改片尾。

    无指纹、指纹相符、源文件 stat 不到这三种仍沿用缓存（与 cache_matches 同一规则）。
    """
    ms = load_tool("merge_segments")
    db_file = tmp_path / "library.db"
    cache_dir = tmp_path / "ocr"
    cache_dir.mkdir()
    monkeypatch.setattr(ms, "DB", str(db_file))
    monkeypatch.setattr(ms, "CACHE", str(cache_dir))
    monkeypatch.setattr(ms, "VLM", str(tmp_path / "no-vlm.jsonl"))

    stale = _video(tmp_path, "ep-stale.mkv", b"stale-src" * 8, 1_800_000_000)
    legacy = _video(tmp_path, "ep-legacy.mkv", b"legacy" * 8, 1_800_000_100)
    match = _video(tmp_path, "ep-match.mkv", b"match" * 8, 1_700_000_000)
    gone = tmp_path / "missing" / "gone.mkv"
    st_match = match.stat()

    lib = Library(db_file)
    sid = lib.upsert_show(path=str(tmp_path / "show"), folder=str(tmp_path / "show"),
                          title="Northwind", year=2020)
    ids = {}
    for key, path in (("stale", stale), ("legacy", legacy), ("match", match), ("gone", gone)):
        ids[key] = lib.upsert_episode(
            show_id=sid, season_number=1, episode_number=len(ids) + 1,
            path=str(path), video_name=path.name, ext="mkv",
            title=f"Northwind {key}", size=path.stat().st_size if path.exists() else 0,
            mtime=1.0)
        lib.save_segments(ids[key], None, (1000.0, 1500.0), "fingerprint")
        row = lib._conn.execute("SELECT id FROM episodes WHERE path=?", (str(path),)).fetchone()
        ids[key] = row["id"]
    lib.close()

    (cache_dir / f"{ids['stale']}.json").write_text(
        json.dumps(_ocr_payload({"size": 1, "mtime": 1.0})), encoding="utf-8")
    (cache_dir / f"{ids['legacy']}.json").write_text(
        json.dumps(_ocr_payload(None)), encoding="utf-8")
    (cache_dir / f"{ids['match']}.json").write_text(
        json.dumps(_ocr_payload({"size": st_match.st_size, "mtime": st_match.st_mtime})),
        encoding="utf-8")
    (cache_dir / f"{ids['gone']}.json").write_text(
        json.dumps(_ocr_payload({"size": 1, "mtime": 1.0})), encoding="utf-8")

    changes = ms.plan()
    assert [c["eid"] for c in changes] == [ids["legacy"], ids["match"], ids["gone"]]
    assert [c["credits"][0] for c in changes] == [1411.0, 1411.0, 1411.0]
    assert str(ids["stale"]) in capsys.readouterr().err


def test_t2_batch_targets_skips_ocr_cache_fingerprint_mismatch(tmp_path, monkeypatch, capsys):
    """换源后不得用过期 OCR 缓存决定要不要打付费 VLM。

    指纹不符：无论旧 decide() 是 None 还是有片尾，都不进待判名单，并告警。
    无指纹、指纹相符且认不出、源文件 stat 不到：仍按原规则进名单。
    指纹相符且真 decide() 认得出：不进名单。urlopen 打桩，禁止真请求。
    """
    mod = load_tool("vlm_credits")
    cache_dir = tmp_path / "ocr"
    cache_dir.mkdir()
    db_file = tmp_path / "lib.db"
    con = sqlite3.connect(db_file)
    con.execute("CREATE TABLE episodes (id INTEGER PRIMARY KEY, path TEXT, missing INTEGER NOT NULL DEFAULT 0)")
    con.execute("CREATE TABLE segments (episode_id INTEGER PRIMARY KEY, credits_start REAL, source TEXT)")

    def add(eid, path, file_fp, frames):
        con.execute("INSERT INTO episodes (id, path, missing) VALUES (?, ?, 0)", (eid, str(path)))
        con.execute("INSERT INTO segments (episode_id, credits_start, source) VALUES (?, NULL, 'fingerprint')",
                    (eid,))
        (cache_dir / f"{eid}.json").write_text(
            json.dumps(_ocr_payload(file_fp, frames)), encoding="utf-8")

    stale_none = _video(tmp_path, "stale-none.mkv", b"a" * 40, 1_800_000_000)
    match_none = _video(tmp_path, "match-none.mkv", b"b" * 40, 1_700_000_000)
    legacy = _video(tmp_path, "legacy.mkv", b"c" * 40, 1_700_000_100)
    gone = tmp_path / "missing" / "gone.mkv"
    match_hit = _video(tmp_path, "match-hit.mkv", b"d" * 48, 1_700_000_200)
    stale_hit = _video(tmp_path, "stale-hit.mkv", b"e" * 48, 1_800_000_200)
    st_none = match_none.stat()
    st_hit = match_hit.stat()
    empty = []
    add(1, stale_none, {"size": 1, "mtime": 1.0}, empty)
    add(2, match_none, {"size": st_none.st_size, "mtime": st_none.st_mtime}, empty)
    add(3, legacy, None, empty)
    add(4, gone, {"size": 1, "mtime": 1.0}, empty)
    add(5, match_hit, {"size": st_hit.st_size, "mtime": st_hit.st_mtime}, _credit_frames())
    add(6, stale_hit, {"size": 1, "mtime": 1.0}, _credit_frames())
    con.commit()
    con.close()

    monkeypatch.setattr(mod, "DB", str(db_file))
    real_expand = os.path.expanduser

    def expand(path):
        if path == "~/Library/Caches/HomeCinema/ocr_tail":
            return str(cache_dir)
        return real_expand(path)

    monkeypatch.setattr(mod.os.path, "expanduser", expand)

    def refuse_network(*_a, **_k):
        raise AssertionError("vlm API must not be called")

    monkeypatch.setattr(mod.urllib.request, "urlopen", refuse_network)

    assert mod.batch_targets() == [2, 3, 4]
    err = capsys.readouterr().err
    assert "集 1 " in err and "集 6 " in err
