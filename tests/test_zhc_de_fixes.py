"""Kimi 审查复核：每条发现一个直接调用真函数的复现测试，修复前应失败。

约定沿用本仓库测试：不起子进程（ffprobe/ffmpeg 一律打桩，见 test_audit_day_hc2.py），
只测判定/解析逻辑，虚构片名。
"""

from __future__ import annotations

import json
import os
import stat as stat_mod
import time
from datetime import datetime, timezone

import pytest

from homecinema.db import Library, utcnow
from homecinema import scanner
from homecinema.scanner import ScanResult, MovieFile, _file_stat, apply_scan, run_scan


# ---------- D-1 _file_stat 用 min(birthtime, mtime) ----------

has_birthtime = hasattr(os.stat(__file__), "st_birthtime")


@pytest.mark.skipif(not has_birthtime, reason="本文件系统不支持 birthtime")
def test_d1_min_is_noop_birthtime_never_exceeds_backdated_mtime(tmp_path):
    """D-1 复核（判不成立）：macOS/APFS 维持 birthtime ≤ mtime——utime 把 mtime 设到
    过去时 birthtime 也被一起拉回过去（实测），tar/cp 同理。「birthtime=今天、
    mtime=2020」的状态造不出来，min(birthtime, mtime) 恒等于 birthtime，
    与 docstring「文件创建时间，取不到用 mtime」一致，不会取到「更旧的 mtime」。"""
    video = tmp_path / "Old Movie (2020).mp4"
    video.write_bytes(b"x")
    old = datetime(2020, 6, 1, tzinfo=timezone.utc).timestamp()
    os.utime(video, (old, old))
    st = video.stat()
    assert st.st_birthtime <= st.st_mtime  # 系统把 birthtime 一起带回过去

    size, mtime, added = _file_stat(video)

    assert mtime == old
    # added 就是 birthtime（min 不改写结果）
    assert datetime.fromisoformat(added).timestamp() == pytest.approx(st.st_birthtime, abs=1e-6)


# ---------- D-2 扫描对单文件/单目录的瞬时错误零防护 ----------

def _mk_movie(root, name, ext="mp4"):
    (root / name).mkdir(parents=True, exist_ok=True)
    (root / name / f"{name}.{ext}").write_bytes(b"")


def _mk_ep(series, season, filename):
    d = series / season if season else series
    d.mkdir(parents=True, exist_ok=True)
    (d / filename).write_bytes(b"")


# root 无视权限位：chmod 0o111 后照样能列目录，复现不出 PermissionError
needs_non_root = pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                                    reason="root 无视目录权限位，造不出不可读目录")


def _lock(path):
    """--x--x--x：stat 能过（is_dir True），列目录 PermissionError（非 root）。"""
    path.chmod(0o111)
    return path


@needs_non_root
def test_d2_unreadable_movie_folder_does_not_abort_scan(tmp_path, cfg, lib):
    movies = tmp_path / "movies"
    _mk_movie(movies, "Good Movie (2001)")
    folder = movies / "Locked Movie (2020)"
    folder.mkdir()
    (folder / "Locked Movie (2020).mp4").write_bytes(b"")
    locked = _lock(folder)
    _mk_ep(tmp_path / "tv" / "Sample Show", "Season 01", "Sample Show S01E01.mkv")
    try:
        stats = run_scan(cfg, lib)  # 修复前：PermissionError 冒泡出 run_scan，整轮中止
        assert stats["movies"] == 1
        assert stats["episodes"] == 1
    finally:
        locked.chmod(0o755)


@needs_non_root
def test_d2_unreadable_season_dir_does_not_abort_scan(tmp_path, cfg, lib):
    tv = tmp_path / "tv"
    _mk_ep(tv / "Good Show", "Season 01", "Good Show S01E01.mkv")
    season = tv / "Locked Show" / "Season 01"
    season.mkdir(parents=True)
    (season / "Locked Show S01E01.mkv").write_bytes(b"")
    locked = _lock(season)
    try:
        stats = run_scan(cfg, lib)  # 修复前：中断；修复后：只丢这一季
        assert stats["episodes"] == 1
        assert [s["title"] for s in lib.list_shows()] == ["Good Show"]
    finally:
        locked.chmod(0o755)


@needs_non_root
def test_d2_unreadable_series_dir_does_not_abort_scan(tmp_path, cfg, lib):
    tv = tmp_path / "tv"
    _mk_ep(tv / "Good Show", "Season 01", "Good Show S01E01.mkv")
    series = tv / "Locked Show"
    series.mkdir()
    (series / "Locked Show S01E01.mkv").write_bytes(b"")
    locked = _lock(series)
    try:
        stats = run_scan(cfg, lib)  # 修复前：中断；修复后：只丢这部剧
        assert stats["episodes"] == 1
    finally:
        locked.chmod(0o755)


def test_d2_stat_error_on_one_movie_does_not_abort_scan(tmp_path, cfg, lib, monkeypatch):
    movies = tmp_path / "movies"
    _mk_movie(movies, "Good Movie (2001)")
    _mk_movie(movies, "Flaky Movie (2020)")
    real = scanner._file_stat

    def flaky(path):
        if "Flaky" in path.name:  # 模拟 iterdir 与 stat 之间文件被移走
            raise FileNotFoundError(2, "扫描中被移走", str(path))
        return real(path)

    monkeypatch.setattr(scanner, "_file_stat", flaky)
    stats = run_scan(cfg, lib)  # 修复前：FileNotFoundError 中止整轮
    assert stats["movies"] == 1


# ---------- D-3 mark_missing_* 先查后写，跨进程并发扫描互相翻状态 ----------

def test_d3_scan_does_not_flip_movie_upserted_during_scan(tmp_path, cfg, lib, monkeypatch):
    """进程 A 扫描期间，进程 B upsert 确认了一部在架片（updated_at 刷新）；
    A 的快照里没有它 → 修复前 mark_missing 把它翻成 missing，在架片从列表消失。"""
    db = lib
    mid = _upsert(db, "Back.mkv")
    real_scan_paths = scanner.scan_paths

    def racing_scan_paths(config):
        _upsert(db, "Back.mkv", size=2, mtime=2.0)  # B：另一进程扫描中确认在架
        return real_scan_paths(config)  # A：快照里没有这部片（空库目录）

    monkeypatch.setattr(scanner, "scan_paths", racing_scan_paths)
    run_scan(cfg, lib)
    assert db.get_movie(mid)["missing"] == 0  # 修复前：missing=1


def _upsert(db, name, size=1, mtime=1.0):
    return db.upsert_movie(path="/m/" + name, folder="/m", video_name=name, ext="mkv",
                           title=name, year=None, size=size, mtime=mtime)


def test_d3_mark_missing_stale_rows_only(lib):
    """直测 mark_missing_movies：before 之后被并发写过的路径不标；
    before 之前就在、这轮没见到的照常标；返回值算实际标记数。"""
    db = lib
    seen_id = _upsert(db, "Seen.mkv")      # A 扫到了，在 seen 里
    back_id = _upsert(db, "Back.mkv")      # A 没扫到；扫描期间 B 又 upsert 它
    gone_id = _upsert(db, "Gone.mkv")      # A 没扫到；之后没人写过 → 真消失
    time.sleep(0.001)  # utcnow 只有微秒精度，保证上面三条的 updated_at 严格早于扫描开始
    started = utcnow()  # 进程 A 扫描开始
    _upsert(db, "Back.mkv", size=2, mtime=2.0)  # B：扫描期间确认在架（updated_at > started）
    marked = db.mark_missing_movies({"/m/Seen.mkv"}, before=started)
    assert marked == 1
    assert db.get_movie(seen_id)["missing"] == 0
    assert db.get_movie(back_id)["missing"] == 0   # 修复前：被翻成 missing=1
    assert db.get_movie(gone_id)["missing"] == 1   # 真消失的照常标


# ---------- D-4 read_overrides 对非法值不兜底 ----------

def test_d4_read_overrides_bad_values_skipped(tmp_path):
    """overrides.toml 值写错（非整数）/节写错（不是表）→ 跳过并继续，
    不能让 fetch-metadata 以 traceback 崩溃。"""
    from homecinema.config import Config
    f = tmp_path / "overrides.toml"
    f.write_text(
        'movie = { "A (2001)" = "abc", "B (2002)" = 42, "C (2003)" = 7.9,'
        ' "D (2004)" = "88", "E (2005)" = true }\n'
        'tv = "oops"\n', encoding="utf-8")
    ov = Config(overrides_file=str(f)).read_overrides()
    assert ov == {"movie": {"B (2002)": 42, "D (2004)": 88}, "tv": {}}


def test_d4_read_overrides_missing_file_still_empty(tmp_path):
    from homecinema.config import Config
    ov = Config(overrides_file=str(tmp_path / "nope.toml")).read_overrides()
    assert ov == {"movie": {}, "tv": {}}


# ---------- E-2 ocr_credits 缓存只按集 id、不校验文件变更 ----------

def _write_cache(cache_dir, eid, payload):
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / f"{eid}.json").write_text(json.dumps(payload), encoding="utf-8")


def test_e2_load_or_extract_revalidates_changed_file(monkeypatch, tmp_path):
    """换源（重编码/换版本，路径与集 id 不变）后不得沿用旧帧特征：
    缓存记下源文件 size+mtime，对不上就重算并写回新指纹。"""
    from homecinema import ocr_credits as oc
    video = tmp_path / "ep07.mkv"
    video.write_bytes(b"x" * 10)
    os.utime(video, (1000000000, 1000000000))
    cache_dir = tmp_path / "cache"
    old = {"duration": 1400.0, "start": 1100.0, "step": 2.0,
           "frames": [{"t": 1101.0, "dark": 0.9, "lines": []}],
           "file": {"size": 10, "mtime": 1000000000.0}}
    _write_cache(cache_dir, 7, old)
    calls = []
    fresh = {"duration": 1400.0, "start": 1100.0, "step": 2.0, "frames": [], "fresh": True}
    monkeypatch.setattr(oc, "extract", lambda *a, **k: calls.append(a) or fresh)

    # 未换源：命中缓存，不重截帧
    assert oc.load_or_extract(str(cache_dir), 7, str(video), 1400.0) == old
    assert calls == []

    # 换源：size/mtime 变了 → 重算（修复前：KeyError 'fresh'，原样返回旧缓存）
    video.write_bytes(b"y" * 20)
    os.utime(video, (2000000000, 2000000000))
    assert oc.load_or_extract(str(cache_dir), 7, str(video), 1400.0)["fresh"] is True
    assert len(calls) == 1
    # 新指纹写回缓存，下次命中不再重算
    saved = json.loads((cache_dir / "7.json").read_text(encoding="utf-8"))
    assert saved["file"] == {"size": 20, "mtime": 2000000000.0}
    assert oc.load_or_extract(str(cache_dir), 7, str(video), 1400.0)["fresh"] is True
    assert len(calls) == 1


def test_e2_legacy_cache_without_fingerprint_still_used(tmp_path):
    """旧格式缓存（无 file 指纹）维持原样使用：不因此对存量库做一次性全量重截帧。"""
    from homecinema import ocr_credits as oc
    video = tmp_path / "ep08.mkv"
    video.write_bytes(b"x")
    cache_dir = tmp_path / "cache"
    legacy = {"duration": 900.0, "start": 600.0, "step": 2.0, "frames": []}
    _write_cache(cache_dir, 8, legacy)
    assert oc.load_or_extract(str(cache_dir), 8, str(video), 900.0) == legacy


# ---------- E-3 recommend.generate 对缓存里的非 dict 元素直接崩 ----------

def _seed_source(lib, name, tmdb_id):
    mid = lib.upsert_movie(path="/movies/" + name, folder="/movies/" + name,
                           video_name=name + ".mp4", ext="mp4", title=name,
                           year=2000, size=0, mtime=0.0)
    lib.update_movie_metadata(mid, {"tmdb_id": tmdb_id, "status": "matched"})
    return mid


def test_e3_generate_skips_non_dict_cache_entries(cfg, lib):
    """缓存 results 混入非 dict 元素（手工编辑/被其他脚本写坏）→ 跳过继续，
    不能让一个坏缓存文件中止整轮推荐。"""
    from homecinema.recommend import generate
    _seed_source(lib, "Poisoned Source", 601)
    _seed_source(lib, "Good Source", 602)
    recs = cfg.cache_dir_path / "recs"
    recs.mkdir(parents=True)
    (recs / "movie_601.json").write_text(json.dumps(
        {"fetched_at": utcnow(), "results": ["junk", 42, None]}), encoding="utf-8")
    (recs / "movie_602.json").write_text(json.dumps(
        {"fetched_at": utcnow(),
         "results": [{"id": 9001, "title": "Kept", "original_title": "Kept",
                      "release_date": "2015-01-01", "vote_count": 500,
                      "vote_average": 7.5, "adult": False}]}), encoding="utf-8")

    class NoNetClient:  # 缓存齐全不会发请求；万一发了也当空
        def recommendations(self, kind, tmdb_id):
            return []

        def download_image(self, *a, **k):
            raise AssertionError("候选没有 poster_path，不该下载图片")

    stats = generate(lib, NoNetClient(), cfg.cache_dir_path, cfg.images_dir)
    assert [r["title"] for r in lib.list_recommendations()] == ["Kept"]  # 修复前：AttributeError 中止
    assert stats["sources"] == 2


# ---------- E-4 embedded_status 外挂字幕同名前缀匹配大小写敏感 ----------

def _no_probe(*args, **kwargs):  # 测试不起子进程：ffprobe 一律打桩成缺失
    raise FileNotFoundError(2, "No such file or directory")


def test_e4_external_sub_prefix_match_case_insensitive(tmp_path, monkeypatch):
    """下载器常见的全小写字幕名（show.s01e01.…）要能对上 Show.S01E01.… 的视频：
    扩展名判断已 lower()，前缀判断却大小写敏感 → 漏判已有中文字幕。"""
    from homecinema import subtitles as sm
    monkeypatch.setattr(sm.subprocess, "run", _no_probe)
    video = tmp_path / "Show.S01E01.1080p.mkv"
    video.write_bytes(b"")
    (tmp_path / "show.s01e01.1080p.chs.srt").write_bytes(b"")
    status = sm.embedded_status(str(video))
    assert status["external"] == ["show.s01e01.1080p.chs.srt"]  # 修复前：[]
    assert status["has_zh"] is True  # 修复前：False，前端误报无中文字幕


# ---------- E-5 embedded_status 直接调 "ffprobe"，无 Homebrew 回退 ----------

def test_e5_embedded_status_resolves_ffprobe_via_tool(monkeypatch):
    """ffprobe 必须经 segments._tool 解析（PATH 优先、Homebrew 目录兜底），
    不能直接 exec "ffprobe"——launchd/图形壳启动的服务 PATH 常不含 /opt/homebrew/bin，
    FileNotFoundError 又被吞掉，内封字幕静默漏检。"""
    from homecinema import subtitles as sm
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(sm, "_tool", lambda name: "/opt/homebrew/bin/" + name)
    monkeypatch.setattr(sm.subprocess, "run", fake_run)
    status = sm.embedded_status("/nonexistent/video.mkv")
    assert captured["cmd"][0] == "/opt/homebrew/bin/ffprobe"  # 修复前：sm 无 _tool
    assert status["embedded"] == []


def test_e5_embedded_status_falls_back_to_homebrew(monkeypatch):
    """真实回退路径：PATH 里没有、shutil.which 找不到时，落到 /opt/homebrew/bin/ffprobe。"""
    import shutil
    from homecinema import subtitles as sm
    if not os.path.exists("/opt/homebrew/bin/ffprobe"):
        pytest.skip("本机没有 Homebrew ffprobe")
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setenv("PATH", "/nonexistent-bin")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(sm.subprocess, "run", fake_run)
    sm.embedded_status("/nonexistent/video.mkv")
    assert captured["cmd"][0] == "/opt/homebrew/bin/ffprobe"


# ---------- E-6 ocr_credits.extract 直接调 "ffmpeg"，同样缺 Homebrew 回退 ----------

def test_e6_extract_resolves_ffmpeg_via_tool(monkeypatch):
    """ffmpeg 也要经 _tool 解析：批处理进程 PATH 不含 /opt/homebrew/bin 时
    逐集 FileNotFoundError、整批零产出。"""
    import subprocess as subprocess_mod
    from homecinema import ocr_credits as oc
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return subprocess_mod.CompletedProcess(cmd, returncode=0)  # 无输出 → 零帧

    monkeypatch.setattr(oc, "_tool", lambda name: "/opt/homebrew/bin/" + name)
    monkeypatch.setattr(oc.subprocess, "run", fake_run)
    data = oc.extract("/nonexistent/video.mkv", 1200.0)
    assert captured["cmd"][0] == "/opt/homebrew/bin/ffmpeg"  # 修复前：oc 无 _tool
    assert data["frames"] == []


def test_e6_extract_falls_back_to_homebrew(monkeypatch):
    import shutil
    import subprocess as subprocess_mod
    from homecinema import ocr_credits as oc
    if not os.path.exists("/opt/homebrew/bin/ffmpeg"):
        pytest.skip("本机没有 Homebrew ffmpeg")
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return subprocess_mod.CompletedProcess(cmd, returncode=0)

    monkeypatch.setenv("PATH", "/nonexistent-bin")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(oc.subprocess, "run", fake_run)
    oc.extract("/nonexistent/video.mkv", 1200.0)
    assert captured["cmd"][0] == "/opt/homebrew/bin/ffmpeg"


# ---------- 验收补：D-2 跳过子目录后，根目录中途消失必须整轮中止 ----------

def test_d2_root_vanishing_mid_scan_aborts_without_marking_missing(tmp_path, cfg, lib, monkeypatch):
    """外接盘扫描途中卸载：子目录逐个 OSError 被跳过会让整库标 missing。
    开始时在、扫完不在的根目录 → run_scan 抛 OSError，库不动。"""
    import shutil
    movies = tmp_path / "movies"
    _mk_movie(movies, "Kept Movie (2001)")
    run_scan(cfg, lib)
    mid = lib.list_movies()[0]["id"]
    real_scan_paths = scanner.scan_paths

    def unmount_mid_scan(config):
        result = real_scan_paths(config)
        shutil.rmtree(movies)  # 模拟扫描途中卷被卸载
        result.movies.clear()  # 子目录全被跳过
        return result

    monkeypatch.setattr(scanner, "scan_paths", unmount_mid_scan)
    with pytest.raises(OSError):
        run_scan(cfg, lib)
    assert lib.get_movie(mid)["missing"] == 0


def test_d3_null_updated_at_still_marked(lib):
    """updated_at 为 NULL 的旧行（理论上的存量）不能因 NULL < x 恒假而永远标不了 missing。"""
    mid = _upsert(lib, "Legacy.mkv")
    lib._conn.execute("UPDATE movies SET updated_at=NULL WHERE id=?", (mid,))
    lib._conn.commit()
    assert lib.mark_missing_movies(set(), before=utcnow()) == 1
    assert lib.get_movie(mid)["missing"] == 1
