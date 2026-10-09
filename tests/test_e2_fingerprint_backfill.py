"""Kimi-E-2 后续（2026-10-07）：community._duration 走指纹校验；存量缓存一次性补指纹脚本。

不起子进程，probe_duration 打桩；虚构片名。
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import time
from pathlib import Path

from homecinema import community, detect

TOOL = Path(__file__).resolve().parent.parent / "tools" / "backfill_ocr_fingerprint.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("backfill_ocr_fingerprint", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _video(tmp_path, name="ep01.mkv", data=b"x" * 10, mtime=1_000_000_000):
    v = tmp_path / name
    v.write_bytes(data)
    os.utime(v, (mtime, mtime))
    return v


def _cache(cache_dir, eid, payload, mtime=None):
    cache_dir.mkdir(parents=True, exist_ok=True)
    f = cache_dir / f"{eid}.json"
    f.write_text(json.dumps(payload), encoding="utf-8")
    if mtime is not None:
        os.utime(f, (mtime, mtime))
    return f


# ---------- community._duration ----------

def test_duration_ignores_cache_whose_fingerprint_mismatches(monkeypatch, tmp_path):
    """换源后缓存指纹对不上：不得把旧 duration 发给 TheIntroDB，改走 ffprobe。"""
    v = _video(tmp_path)
    cache_dir = tmp_path / "ocr"
    _cache(cache_dir, 5, {"duration": 1400.0, "file": {"size": 99, "mtime": 1.0}})
    monkeypatch.setattr(detect, "probe_duration", lambda p: 1234.0)
    assert community._duration({"id": 5, "path": str(v)}, str(cache_dir)) == 1234.0


def test_duration_uses_cache_when_fingerprint_matches(monkeypatch, tmp_path):
    v = _video(tmp_path)
    st = v.stat()
    cache_dir = tmp_path / "ocr"
    _cache(cache_dir, 6, {"duration": 1400.0,
                          "file": {"size": st.st_size, "mtime": st.st_mtime}})
    monkeypatch.setattr(detect, "probe_duration", lambda p: (_ for _ in ()).throw(AssertionError))
    assert community._duration({"id": 6, "path": str(v)}, str(cache_dir)) == 1400.0


def test_duration_legacy_cache_and_missing_source_still_used(monkeypatch, tmp_path):
    """与 load_or_extract 同一规则：旧格式无指纹、源 stat 不到 → 沿用缓存。"""
    v = _video(tmp_path)
    cache_dir = tmp_path / "ocr"
    _cache(cache_dir, 7, {"duration": 900.0})
    _cache(cache_dir, 8, {"duration": 800.0, "file": {"size": 1, "mtime": 1.0}})
    monkeypatch.setattr(detect, "probe_duration", lambda p: 1.0)
    assert community._duration({"id": 7, "path": str(v)}, str(cache_dir)) == 900.0
    assert community._duration({"id": 8, "path": str(tmp_path / "gone.mkv")},
                               str(cache_dir)) == 800.0


def test_duration_non_dict_cache_falls_back(monkeypatch, tmp_path):
    v = _video(tmp_path)
    cache_dir = tmp_path / "ocr"
    _cache(cache_dir, 9, [1, 2])
    monkeypatch.setattr(detect, "probe_duration", lambda p: 1500.0)
    assert community._duration({"id": 9, "path": str(v)}, str(cache_dir)) == 1500.0


# ---------- tools/backfill_ocr_fingerprint.py ----------

def _db(tmp_path, rows):
    p = tmp_path / "lib.db"
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE episodes (id INTEGER PRIMARY KEY, path TEXT)")
    con.executemany("INSERT INTO episodes VALUES (?, ?)", rows)
    con.commit()
    con.close()
    return str(p)


def test_backfill_buckets_dry_run_then_apply(tmp_path):
    tool = _load_tool()
    now = time.time()
    good = _video(tmp_path, "good.mkv", mtime=1_000_000_000)
    changed = _video(tmp_path, "changed.mkv")  # 源 mtime/ctime 晚于缓存写入 → 可疑
    has = _video(tmp_path, "has.mkv")
    st_has = has.stat()
    cache_dir = tmp_path / "ocr"
    legacy = {"duration": 1400.0, "start": 1100.0, "step": 2.0, "frames": [{"t": 1.0}]}
    f_good = _cache(cache_dir, 1, legacy, mtime=now + 100)
    f_changed = _cache(cache_dir, 2, legacy, mtime=1_000_000_000)
    os.utime(changed, (now, now))
    _cache(cache_dir, 3, legacy, mtime=now + 100)                       # 源已删
    _cache(cache_dir, 4, legacy, mtime=now + 100)                       # 未挂载卷
    _cache(cache_dir, 5, legacy, mtime=now + 100)                       # 库里无此集
    _cache(cache_dir, 6, {**legacy, "file": {"size": st_has.st_size,
                                             "mtime": st_has.st_mtime}})
    _cache(cache_dir, 7, {**legacy, "file": {"size": 1, "mtime": 1.0}})  # 已有但不符
    (cache_dir / "8.json.tmp").write_text("{}")                         # 非缓存文件忽略
    db = _db(tmp_path, [(1, str(good)), (2, str(changed)), (3, str(tmp_path / "gone.mkv")),
                        (4, "/Volumes/NoSuchVolume_hc_test/TV/x.mkv"),
                        (6, str(has)), (7, str(has))])
    good_before = f_good.read_bytes()

    counts, detail = tool.run(str(cache_dir), db, apply=False)
    assert counts["total"] == 7
    assert (counts["fill"], counts["suspicious"], counts["missing"], counts["unmounted"],
            counts["orphan"], counts["has_ok"], counts["has_mismatch"]) == (1, 1, 1, 1, 1, 1, 1)
    assert counts["written"] == 0 and f_good.read_bytes() == good_before  # dry-run 不写
    assert "src_mtime>cache_mtime" in detail["suspicious"][2]["reasons"]
    assert detail["unmounted"][4]["volume"] == "/Volumes/NoSuchVolume_hc_test"

    counts, _ = tool.run(str(cache_dir), db, apply=True)
    assert counts["written"] == 1
    saved = json.loads(f_good.read_text(encoding="utf-8"))
    st = good.stat()
    assert saved == {**legacy, "file": {"size": st.st_size, "mtime": st.st_mtime}}
    assert f_good.stat().st_mtime == now + 100       # 保留缓存原 mtime
    assert "file" not in json.loads(f_changed.read_text(encoding="utf-8"))  # 可疑不补
    assert json.loads((cache_dir / "7.json").read_text())["file"] == {"size": 1, "mtime": 1.0}
    assert not list(cache_dir.glob("*.fpfill.tmp"))

    # 补完后 load_or_extract / _duration 认它为「与源一致」；再跑一遍幂等
    from homecinema.ocr_credits import cache_matches
    assert cache_matches(saved, good.stat())
    counts, _ = tool.run(str(cache_dir), db, apply=True)
    assert counts["fill"] == 0 and counts["has_ok"] == 2
