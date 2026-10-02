"""存储（segments 表）与 detect-segments 批处理：一律虚构片名、假 ffprobe、假声纹接口。"""

from __future__ import annotations

import pytest

from homecinema import detect
from homecinema.scanner import run_scan


def build_tree(tmp_path, seasons=((1, 3),), movie=False):
    if movie:
        folder = tmp_path / "movies" / "Example Movie (2001)"
        folder.mkdir(parents=True)
        (folder / "Example Movie (2001).mp4").write_bytes(b"")
    show = tmp_path / "tv" / "Sample Show"
    for season_number, count in seasons:
        sdir = show / f"Season {season_number:02d}"
        sdir.mkdir(parents=True)
        for n in range(1, count + 1):
            (sdir / f"Sample Show S{season_number:02d}E{n:02d}.mkv").write_bytes(b"")


def show_id(lib) -> int:
    return lib.list_shows()[0]["id"]


# ---------- 存储 ----------

def test_save_and_get_segments(tmp_path, cfg, lib):
    build_tree(tmp_path, seasons=((1, 2),))
    run_scan(cfg, lib)
    ep = lib.episodes_for_show(show_id(lib))[0]

    lib.save_segments(ep["id"], (5.0, 65.0), (1400.0, 1480.0), "fingerprint")
    row = lib.get_segments(ep["id"])
    assert row["intro_start"] == 5.0 and row["intro_end"] == 65.0
    assert row["credits_start"] == 1400.0 and row["credits_end"] == 1480.0
    assert row["source"] == "fingerprint"
    assert row["detected_at"]

    # 同集重跑覆盖旧记录
    lib.save_segments(ep["id"], None, None, "none")
    row = lib.get_segments(ep["id"])
    assert row["intro_start"] is None and row["credits_start"] is None
    assert row["source"] == "none"

    assert lib.get_segments(9999) is None


def test_seasons_needing_segments_returns_whole_season(tmp_path, cfg, lib):
    build_tree(tmp_path, seasons=((1, 3),))
    run_scan(cfg, lib)
    sid = show_id(lib)
    eps = lib.episodes_for_show(sid)

    need = lib.seasons_needing_segments()
    assert len(need) == 1 and need[0][0] == sid and need[0][1] == 1
    assert [e["id"] for e in need[0][2]] == [e["id"] for e in eps]

    # 3 集里已检测 2 集 → 仍返回整季全部 3 集
    lib.save_segments(eps[0]["id"], (1.0, 2.0), None, "chapters")
    lib.save_segments(eps[1]["id"], (1.0, 2.0), None, "chapters")
    need = lib.seasons_needing_segments()
    assert len(need) == 1
    assert [e["id"] for e in need[0][2]] == [e["id"] for e in eps]

    # 整季检测完 → 不再返回；show_id 过滤同样为空
    lib.save_segments(eps[2]["id"], None, None, "none")
    assert lib.seasons_needing_segments() == []
    assert lib.seasons_needing_segments(show_id=sid) == []


def test_seasons_needing_segments_show_filter(tmp_path, cfg, lib):
    build_tree(tmp_path, seasons=((1, 2),))
    # 第二部剧
    other = tmp_path / "tv" / "Other Show" / "Season 01"
    other.mkdir(parents=True)
    (other / "Other Show S01E01.mkv").write_bytes(b"")
    run_scan(cfg, lib)
    shows = {s["title"]: s["id"] for s in lib.list_shows()}
    need = lib.seasons_needing_segments(show_id=shows["Other Show"])
    assert len(need) == 1 and need[0][0] == shows["Other Show"]
    assert len(lib.seasons_needing_segments()) == 2


# ---------- detect：假 pp / 假声纹 ----------

class Fake:
    """集中记录探测调用，并给出可配置的章节/声纹返回值。"""

    def __init__(self, monkeypatch, durations=None, chapter_intros=None,
                 detect_result=None):
        self.durations = durations or {}
        self.chapter_intros = chapter_intros or {}
        self.detect_result = detect_result  # 默认 {key: (100,160)}
        self.probed: list[str] = []
        self.fingerprints: list[tuple[str, float, float]] = []
        self.detect_offsets: list[float] = []
        self.progress: list[str] = []

        def probe_duration(path):
            self.probed.append(path)
            if path not in self.durations:
                raise RuntimeError("ffprobe duration 失败")
            return self.durations[path]

        def probe_chapters(path):
            intro = self.chapter_intros.get(path)
            return [{"start_time": intro[0], "end_time": intro[1]}] if intro else []

        def chapters_to_segments(chapters, duration):
            if chapters:
                return {"intro": (chapters[0]["start_time"], chapters[0]["end_time"]),
                        "credits": None}
            return {"intro": None, "credits": None}

        def fingerprint(path, start, length):
            self.fingerprints.append((path, start, length))
            return [1, 2, 3]

        def detect_season(fps, offset_sec=0.0, **kwargs):
            self.detect_offsets.append(offset_sec)
            if self.detect_result is not None:
                return {k: self.detect_result for k in fps}
            return {k: (100.0, 160.0) for k in fps}

        monkeypatch.setattr(detect, "probe_duration", probe_duration)
        monkeypatch.setattr(detect, "probe_chapters", probe_chapters)
        monkeypatch.setattr(detect.segments, "chapters_to_segments", chapters_to_segments)
        monkeypatch.setattr(detect.segments, "fingerprint", fingerprint)
        monkeypatch.setattr(detect.segments, "detect_season", detect_season)

    def run(self, lib, **kwargs):
        return detect.run_detect_segments(
            lib, progress=lambda msg, **kw: self.progress.append(msg), **kwargs)


def test_chapter_values_kept_and_missing_part_filled_by_fingerprint(tmp_path, cfg, lib, monkeypatch):
    """章节只给了片头的集：片头保留章节值，片尾仍由声纹补（真实片库里常有几集只标了 Credits 章节，
    旧逻辑整集跳过声纹，片头就漏了）；该集也参与声纹比对。"""
    build_tree(tmp_path, seasons=((1, 3),))
    run_scan(cfg, lib)
    eps = lib.episodes_for_show(show_id(lib))
    paths = {ep["id"]: ep["path"] for ep in eps}
    e01 = paths[eps[0]["id"]]
    fake = Fake(monkeypatch,
                durations={p: 1200.0 for p in paths.values()},
                chapter_intros={e01: (10.0, 70.0)})
    fake.run(lib)

    fp_paths = {p for p, _, _ in fake.fingerprints}
    assert e01 in fp_paths

    row = lib.get_segments(eps[0]["id"])
    assert row["source"] == "chapters+fingerprint"
    assert row["intro_start"] == 10.0 and row["intro_end"] == 70.0      # 章节值不被声纹覆盖
    assert row["credits_start"] == 1000.0 and row["credits_end"] == 1060.0  # 声纹补片尾：900 + (100,160)

    row2 = lib.get_segments(eps[1]["id"])
    assert row2["source"] == "fingerprint"
    assert row2["intro_start"] == 100.0 and row2["intro_end"] == 160.0


def test_credits_offset_follows_each_episode_duration(tmp_path, cfg, lib, monkeypatch):
    build_tree(tmp_path, seasons=((1, 3),))
    run_scan(cfg, lib)
    eps = lib.episodes_for_show(show_id(lib))
    durations = {eps[0]["path"]: 1200.0, eps[1]["path"]: 1500.0, eps[2]["path"]: 1800.0}
    fake = Fake(monkeypatch, durations=durations)
    fake.run(lib)

    # detect_season 两趟（片头 + 片尾）都以 offset_sec=0 调
    assert fake.detect_offsets and all(o == 0.0 for o in fake.detect_offsets)

    intro_len = {p: length for p, start, length in fake.fingerprints if start == 0.0}
    credit_start = {p: start for p, start, _ in fake.fingerprints if start > 0.0}
    for ep in eps:
        d = durations[ep["path"]]
        assert intro_len[ep["path"]] == min(600.0, d * 0.35)
        assert credit_start[ep["path"]] == d - 300.0
        row = lib.get_segments(ep["id"])
        # 相对秒数 (100,160) 各自加回 D-300
        assert row["credits_start"] == pytest.approx(100.0 + d - 300.0)
        assert row["credits_end"] == pytest.approx(160.0 + d - 300.0)


def test_single_episode_season_skips_fingerprint(tmp_path, cfg, lib, monkeypatch):
    build_tree(tmp_path, seasons=((1, 1),))
    run_scan(cfg, lib)
    ep = lib.episodes_for_show(show_id(lib))[0]
    fake = Fake(monkeypatch, durations={ep["path"]: 1200.0})
    fake.run(lib)
    assert fake.fingerprints == []
    assert lib.get_segments(ep["id"])["source"] == "none"


def test_single_episode_ffprobe_error_saved_as_none(tmp_path, cfg, lib, monkeypatch):
    build_tree(tmp_path, seasons=((1, 2),))
    run_scan(cfg, lib)
    eps = lib.episodes_for_show(show_id(lib))
    # E01 的 ffprobe 全失败（probe_duration 抛异常、chapters 也抛异常）
    fake = Fake(monkeypatch, durations={eps[1]["path"]: 1200.0})

    def boom(path):
        raise RuntimeError("ffprobe 崩了")

    monkeypatch.setattr(detect, "probe_chapters", boom)
    fake.run(lib)  # 不抛出
    assert lib.get_segments(eps[0]["id"])["source"] == "none"
    assert lib.get_segments(eps[1]["id"])["source"] == "fingerprint"


def test_rerun_skips_completed_season(tmp_path, cfg, lib, monkeypatch):
    build_tree(tmp_path, seasons=((1, 2), (2, 2)))
    run_scan(cfg, lib)
    paths = {ep["path"] for ep in lib.episodes_for_show(show_id(lib))}
    fake = Fake(monkeypatch, durations={p: 1200.0 for p in paths})

    assert fake.run(lib, limit_seasons=1) == 1
    assert len(fake.progress) == 1 and "S01" in fake.progress[0]
    assert fake.run(lib) == 1                       # 只剩 S02
    assert len(fake.progress) == 2 and "S02" in fake.progress[1]
    assert fake.run(lib) == 0                       # 全部完成，不再处理


def test_movies_not_processed(tmp_path, cfg, lib, monkeypatch):
    build_tree(tmp_path, seasons=((1, 2),), movie=True)
    run_scan(cfg, lib)
    paths = {ep["path"] for ep in lib.episodes_for_show(show_id(lib))}
    fake = Fake(monkeypatch, durations={p: 1200.0 for p in paths})
    fake.run(lib)
    assert all("Sample Show" in p for p in fake.probed)
    assert not any("Example Movie" in p for p in fake.probed)
    assert all("Sample Show" in line for line in fake.progress)
