"""ocr_credits 补测：缓存指纹失效/截帧失败不丢旧缓存、extract 帧时间推算、
Vision 识别结果解析（假 Quartz/Vision/Foundation 模块注入）、判定边界。

约定沿用本仓库测试：ffmpeg 一律打桩不起子进程，不需要真视频，虚构片名。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import types

import pytest

from homecinema import ocr_credits as oc


def _frame(t, dark=0.95, lines=()):
    return {"t": t, "dark": dark, "lines": [list(x) for x in lines]}


def _credit_lines(t):
    # 每帧名字不同，避免被 _watermarks 当成台标
    tag = f"{chr(65 + t % 26)}{chr(65 + t // 26 % 26)}"
    return ((f"Producer Role{tag}", 0.4, 0.55, 0.2, 0.03),
            (f"PERSON NAME{tag}", 0.4, 0.47, 0.2, 0.05))


def _ep(frames, duration=1400.0, start=1100.0):
    return {"duration": duration, "start": start, "step": 2.0, "frames": frames}


# ---------- load_or_extract：缓存与指纹 ----------

def _write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_extract_failure_keeps_stale_cache_and_writes_nothing(monkeypatch, tmp_path):
    """换源后重算时 ffmpeg 失败：异常上抛（批处理记日志），旧缓存原样保留、不留 .tmp 半成品，
    更不能把指纹改成新文件的（否则下次命中会沿用旧帧特征）。"""
    video = tmp_path / "Glass Harbor S01E03.mkv"
    video.write_bytes(b"new-encode")
    cache = tmp_path / "cache" / "3.json"
    old = {"duration": 1400.0, "start": 1100.0, "step": 2.0, "frames": [],
           "file": {"size": 1, "mtime": 1.0}}
    _write(cache, old)

    def boom(cmd, **kw):
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(oc, "_tool", lambda name: name)
    monkeypatch.setattr(oc.subprocess, "run", boom)
    with pytest.raises(subprocess.CalledProcessError):
        oc.load_or_extract(str(cache.parent), 3, str(video), 1400.0)
    assert json.loads(cache.read_text(encoding="utf-8")) == old
    assert sorted(os.listdir(cache.parent)) == ["3.json"]


def test_unstatable_source_uses_fingerprinted_cache(monkeypatch, tmp_path):
    """源文件 stat 不到（外置盘未挂载）：即便缓存带指纹也沿用，不去截帧（反正截不了）。"""
    cache = tmp_path / "cache" / "5.json"
    cached = {"duration": 1400.0, "start": 1100.0, "step": 2.0, "frames": [],
              "file": {"size": 999, "mtime": 123.0}}
    _write(cache, cached)
    monkeypatch.setattr(oc, "extract", lambda *a, **k: pytest.fail("不应重截帧"))
    got = oc.load_or_extract(str(cache.parent), 5, str(tmp_path / "missing.mkv"), 1400.0)
    assert got == cached


def test_unstatable_source_without_cache_saves_no_fingerprint(monkeypatch, tmp_path):
    """stat 失败但仍走到 extract（例如打桩/网络盘时有时无）：写入的缓存不带 file 指纹，
    不能伪造一个指纹；缓存目录不存在时自动创建。"""
    fresh = {"duration": 900.0, "start": 600.0, "step": 2.0, "frames": []}
    monkeypatch.setattr(oc, "extract", lambda path, dur: dict(fresh))
    cache_dir = tmp_path / "nested" / "cache"
    got = oc.load_or_extract(str(cache_dir), 6, str(tmp_path / "gone.mkv"), 900.0)
    assert "file" not in got
    saved = json.loads((cache_dir / "6.json").read_text(encoding="utf-8"))
    assert saved == fresh


def test_fractional_mtime_roundtrips_through_json(monkeypatch, tmp_path):
    """mtime 是带小数的浮点：写入 JSON 再读回必须逐位相等，否则每次都判成换源、反复重截帧。"""
    video = tmp_path / "Quiet Orbit S02E01.mkv"
    video.write_bytes(b"x" * 33)
    os.utime(video, (1700000000.123456, 1700000000.123456))
    calls = []
    monkeypatch.setattr(oc, "extract", lambda path, dur: calls.append(path) or
                        {"duration": dur, "start": 0.0, "step": 2.0, "frames": []})
    cache_dir = str(tmp_path / "cache")
    first = oc.load_or_extract(cache_dir, 11, str(video), 1300.0)
    assert first["file"] == oc.fingerprint(os.stat(video))
    oc.load_or_extract(cache_dir, 11, str(video), 1300.0)
    assert len(calls) == 1


def test_cache_matches_detects_size_only_change(tmp_path):
    """mtime 被保留（cp -p / rsync -t 换版本）但大小变了：仍判为换源。"""
    f = tmp_path / "a.mkv"
    f.write_bytes(b"12345")
    st = os.stat(f)
    assert oc.cache_matches({"file": {"size": 5, "mtime": st.st_mtime}}, st)
    assert not oc.cache_matches({"file": {"size": 6, "mtime": st.st_mtime}}, st)
    assert not oc.cache_matches({"file": {"size": 5, "mtime": st.st_mtime + 1}}, st)
    # 非 dict 缓存（损坏/旧工具写的列表）不崩，按旧格式沿用
    assert oc.cache_matches([1, 2], st)


@pytest.mark.xfail(strict=True, reason="已知缺陷：损坏的缓存 JSON 直接抛 JSONDecodeError，"
                   "该集每次批处理都报错、永不自愈（应当视为无缓存重算）")
def test_corrupt_cache_json_is_recomputed(monkeypatch, tmp_path):
    cache = tmp_path / "cache" / "9.json"
    cache.parent.mkdir()
    cache.write_text('{"duration": 14', encoding="utf-8")
    video = tmp_path / "v.mkv"
    video.write_bytes(b"x")
    monkeypatch.setattr(oc, "extract", lambda path, dur: {"duration": dur, "start": 0.0,
                                                          "step": 2.0, "frames": []})
    got = oc.load_or_extract(str(cache.parent), 9, str(video), 600.0)
    assert got["frames"] == []


# ---------- extract：截帧命令与帧时间 ----------

def _fake_ffmpeg(names):
    """假 ffmpeg：往输出模板所在目录写指定文件名（乱序给出，检验排序）。"""
    captured = {}

    def run(cmd, **kw):
        captured["cmd"] = cmd
        out_dir = os.path.dirname(cmd[-1])
        for n in names:
            with open(os.path.join(out_dir, n), "wb") as fh:
                fh.write(b"\xff\xd8")
        return subprocess.CompletedProcess(cmd, 0)
    return run, captured


def test_extract_frame_times_order_and_non_jpg_ignored(monkeypatch):
    """帧按文件名排序，第 i 帧时间 = start + (i+0.5)*step；非 .jpg 杂文件不算帧；
    暗像素比例保留三位小数。"""
    run, captured = _fake_ffmpeg(["f00003.jpg", "f00001.jpg", "f00002.jpg", "f00001.txt"])
    seen = []
    monkeypatch.setattr(oc, "_tool", lambda name: name)
    monkeypatch.setattr(oc.subprocess, "run", run)
    monkeypatch.setattr(oc, "_dark_fraction", lambda p: 0.123456)
    monkeypatch.setattr(oc, "recognize", lambda p: seen.append(os.path.basename(p)) or
                        [["Cast", 0.1, 0.2, 0.3, 0.04]])
    data = oc.extract("/nonexistent/Ember Lane S01E02.mkv", 1000.0, tail_sec=300.0, step=2.0)
    assert seen == ["f00001.jpg", "f00002.jpg", "f00003.jpg"]
    assert [f["t"] for f in data["frames"]] == [701.0, 703.0, 705.0]
    assert all(f["dark"] == 0.123 for f in data["frames"])
    assert data["start"] == 700.0 and data["step"] == 2.0 and data["duration"] == 1000.0
    cmd = captured["cmd"]
    assert cmd[cmd.index("-ss") + 1] == "700.000"
    assert "fps=1/2.0" in cmd[cmd.index("-vf") + 1]


def test_extract_short_video_starts_at_zero(monkeypatch):
    """片长短于 tail_sec：起点夹到 0，不能给 ffmpeg 负的 -ss。"""
    run, captured = _fake_ffmpeg(["f00001.jpg"])
    monkeypatch.setattr(oc, "_tool", lambda name: name)
    monkeypatch.setattr(oc.subprocess, "run", run)
    monkeypatch.setattr(oc, "_dark_fraction", lambda p: 1.0)
    monkeypatch.setattr(oc, "recognize", lambda p: [])
    data = oc.extract("/nonexistent/short.mkv", 120.0)
    assert captured["cmd"][captured["cmd"].index("-ss") + 1] == "0.000"
    assert data["start"] == 0.0 and data["frames"][0]["t"] == 1.0


def test_dark_fraction_counts_pixels_below_threshold(tmp_path):
    """真实 JPEG 上算暗像素比例：左半黑、右半白 → 约 0.5。"""
    pytest.importorskip("numpy")
    Image = pytest.importorskip("PIL.Image")
    im = Image.new("L", (320, 180), 255)
    im.paste(0, (0, 0, 160, 180))
    p = tmp_path / "f00001.jpg"
    im.convert("RGB").save(p, quality=95)
    assert oc._dark_fraction(str(p)) == pytest.approx(0.5, abs=0.03)


# ---------- recognize：注入假 Vision 模块 ----------

def _install_fake_vision(monkeypatch, *, src_ok=True, perform_ok=True, results=()):
    class Req:
        def __init__(self):
            self.level = None
            self.correction = None

        @classmethod
        def alloc(cls):
            return cls

        @classmethod
        def init(cls):
            inst = cls.__new__(cls)
            Req.__init__(inst)
            state["req"] = inst
            return inst

        def setRecognitionLevel_(self, lv):
            self.level = lv

        def setUsesLanguageCorrection_(self, v):
            self.correction = v

        def results(self):
            return list(results) if results is not None else None

    class Handler:
        @classmethod
        def alloc(cls):
            return cls()

        def initWithCGImage_options_(self, img, opts):
            return self

        def performRequests_error_(self, reqs, err):
            return perform_ok, None

    state = {}
    quartz = types.ModuleType("Quartz")
    quartz.CGImageSourceCreateWithURL = lambda url, opts: ("src", url) if src_ok else None
    quartz.CGImageSourceCreateImageAtIndex = lambda src, i, opts: "img"
    vision = types.ModuleType("Vision")
    vision.VNRecognizeTextRequest = Req
    vision.VNRequestTextRecognitionLevelFast = 1
    vision.VNImageRequestHandler = Handler
    foundation = types.ModuleType("Foundation")
    foundation.NSURL = types.SimpleNamespace(fileURLWithPath_=lambda p: "file://" + p)
    for name, mod in (("Quartz", quartz), ("Vision", vision), ("Foundation", foundation)):
        monkeypatch.setitem(sys.modules, name, mod)
    return state


def _obs(text, x, y, w, h):
    ns = types.SimpleNamespace
    cand = [ns(string=lambda: text)] if text is not None else []
    box = ns(origin=ns(x=x, y=y), size=ns(width=w, height=h))
    return ns(topCandidates_=lambda n: cand, boundingBox=lambda: box)


def test_recognize_parses_observations_and_skips_empty_candidates(monkeypatch):
    """结果转成 [文字, x, y, w, h] 且坐标保留三位；无候选的观测跳过；快速档、不做语言纠错。"""
    state = _install_fake_vision(monkeypatch, results=[
        _obs("Directed by", 0.41234, 0.55555, 0.2, 0.03333),
        _obs(None, 0.1, 0.1, 0.1, 0.1),
        _obs("MARA VEY", 0.4, 0.47, 0.21119, 0.05),
    ])
    lines = oc.recognize("/tmp/none.jpg")
    assert lines == [["Directed by", 0.412, 0.556, 0.2, 0.033],
                     ["MARA VEY", 0.4, 0.47, 0.211, 0.05]]
    assert state["req"].level == 1 and state["req"].correction is False


@pytest.mark.parametrize("kw", [{"src_ok": False}, {"perform_ok": False}, {"results": None}])
def test_recognize_failure_paths_return_empty(monkeypatch, kw):
    """图片读不出 / Vision 执行失败 / results 为 None：都返回空列表，不抛。"""
    _install_fake_vision(monkeypatch, **kw)
    assert oc.recognize("/tmp/none.jpg") == []


# ---------- 判定边界 ----------

def test_gap_longer_than_limit_splits_block_and_later_block_wins_tie():
    """块内连续 4 帧无字（> MAX_GAP_FRAMES）就断开成两块；两块有字帧数相同时取靠后的那块。"""
    frames = ([_frame(t, dark=0.3) for t in range(1101, 1281, 2)]
              + [_frame(t, lines=_credit_lines(t)) for t in range(1281, 1311, 2)]   # 15 帧
              + [_frame(t, dark=0.95) for t in range(1311, 1319, 2)]                # 4 帧空
              + [_frame(t, lines=_credit_lines(t)) for t in range(1319, 1349, 2)]   # 15 帧
              + [_frame(t, dark=0.95) for t in range(1349, 1399, 2)])
    # duration 1398：两块的结束都落在片尾 90 秒内，都合格，比的是平局取后
    assert oc.decide(_ep(frames, duration=1398.0)) == 1318.0


def test_gap_within_limit_keeps_one_block():
    """3 帧空隙（= MAX_GAP_FRAMES）不断块：起点是第一段的开头。"""
    frames = ([_frame(t, dark=0.3) for t in range(1101, 1281, 2)]
              + [_frame(t, lines=_credit_lines(t)) for t in range(1281, 1311, 2)]
              + [_frame(t, dark=0.95) for t in range(1311, 1317, 2)]
              + [_frame(t, lines=_credit_lines(t)) for t in range(1317, 1399, 2)])
    assert oc.decide(_ep(frames)) == 1280.0


def test_sparse_block_rejected_by_density():
    """有字帧与空帧交替（密度 < MIN_DENSITY）：即便很长也不认。"""
    frames = ([_frame(t, dark=0.3) for t in range(1101, 1201, 2)]
              + [_frame(t, lines=_credit_lines(t) if (t // 2) % 2 == 0 else ())
                 for t in range(1201, 1399, 2)])
    assert oc.decide(_ep(frames)) is None


def test_block_at_window_start_ok_when_window_covers_whole_video():
    """窗口起点就是片头（start == 0）时，块从第一帧开始也能给结论，结果夹到 >= 0。"""
    frames = [_frame(t, lines=_credit_lines(t)) for t in range(1, 41, 2)]
    assert oc.decide(_ep(frames, duration=40.0, start=0.0)) == 0.0


def test_empty_frames_is_none():
    assert oc.decide(_ep([])) is None


def test_credit_frame_subtitle_band_rules():
    """只在底部字幕带的两行：字幕，不算；底部三行以上：算（滚动字幕压低的演职员表）。"""
    low2 = [("Sound Mixer", 0.4, 0.05, 0.2, 0.03), ("ORIN TALLE", 0.4, 0.01, 0.2, 0.03)]
    low3 = low2 + [("Boom Operator", 0.4, 0.09, 0.2, 0.03)]
    assert oc.credit_frame(_frame(0, lines=low2)) is False
    assert oc.credit_frame(_frame(0, lines=low3)) is True


def test_credit_frame_fullwidth_question_and_lyrics_rejected():
    """全角问号结尾、带音符（歌词）的行出现时整帧不算演职员表；纯数字/短串不计入有效行。"""
    base = [("Story Editor", 0.4, 0.55, 0.2, 0.03), ("LIN ASHBY", 0.4, 0.47, 0.2, 0.05)]
    assert oc.credit_frame(_frame(0, lines=base)) is True
    assert oc.credit_frame(_frame(0, lines=base + [("你要去哪里？", 0.4, 0.4, 0.2, 0.03)])) is False
    assert oc.credit_frame(_frame(0, lines=base + [("♪ under the pale moon", 0.4, 0.4, 0.2, 0.03)])) is False
    noise = [("2024", 0.4, 0.55, 0.2, 0.03), ("AB", 0.4, 0.47, 0.2, 0.05), ("LIN ASHBY", 0.4, 0.4, 0.2, 0.05)]
    assert oc.credit_frame(_frame(0, lines=noise)) is False
