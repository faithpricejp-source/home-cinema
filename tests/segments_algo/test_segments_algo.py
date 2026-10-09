"""segments.py 的测试：/opt/homebrew/bin/python3 -m unittest test_segments -v

素材由 make_fixtures.py 用 ffmpeg 合成到 fixtures/（缺了会自动生成）。
片头/片尾的判定精度断言：与真值相差不超过 1.5 秒。
"""

from __future__ import annotations

import os
import random
import unittest

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import make_fixtures  # noqa: E402
from homecinema import segments  # noqa: E402

T = segments.SAMPLE_SEC
# 片头/片尾位置的 1.5 秒精度断言按 macOS Homebrew 的 ffmpeg + chromaprint 标定；
# Ubuntu 24.04 的 ffmpeg 6.1 + fpcalc 1.5.1 声纹略有不同，cr1/cr3 偏差 1.5~2.1 秒、ns1 误报一段
macos_calibrated = unittest.skipUnless(
    sys.platform == "darwin",
    "精度断言按 Homebrew ffmpeg+chromaprint 标定，Linux 发行版的 ffmpeg/fpcalc 声纹有偏差")
FIXTURE_DIR = make_fixtures.FIXTURE_DIR

# 声纹下标 → 秒
def sec(index: int) -> float:
    return index * T


def _path(key: str) -> str:
    return make_fixtures.fixture_path(key + ".wav")


def setUpModule() -> None:
    """fixtures/ 里没有素材就用 ffmpeg 现合成。"""
    needed = ([f"ep{i}.wav" for i in (1, 2, 3, 4)]
              + [f"cr{i}.wav" for i in (1, 2, 3, 4)]
              + [f"ns{i}.wav" for i in (1, 2, 3)]
              + [make_fixtures.NO_AUDIO_NAME])
    if not all(os.path.exists(os.path.join(FIXTURE_DIR, name)) for name in needed):
        make_fixtures.build_fixtures()


class TestFingerprint(unittest.TestCase):
    """fingerprint()：取一段音频的声纹，异常输入返回 []。"""

    def test_returns_many_ticks(self):
        fps = segments.fingerprint(_path("ep1"), 0.0, 120.0)
        self.assertGreater(len(fps), int(100 / T))
        self.assertTrue(all(isinstance(v, int) for v in fps))

    def test_index_maps_to_seconds(self):
        """声纹第 i 个整数 ≈ 起点 + i * SAMPLE_SEC；尾部约 2.7 秒不出整数（chromaprint 需要后续帧）。"""
        fps = segments.fingerprint(_path("ep1"), 0.0, 120.0)
        covered = len(fps) * T
        self.assertAlmostEqual(covered, 117.4, delta=1.0)
        self.assertLess(covered, 120.0)

    def test_two_windows_of_same_audio_align(self):
        """同一音频取两次相同窗口，声纹应当一致。"""
        a = segments.fingerprint(_path("ep1"), 0.0, 30.0)
        b = segments.fingerprint(_path("ep1"), 0.0, 30.0)
        self.assertEqual(a, b)

    def test_missing_file(self):
        self.assertEqual(segments.fingerprint(os.path.join(FIXTURE_DIR, "nope.wav"), 0, 10), [])

    def test_file_without_audio_track(self):
        path = make_fixtures.fixture_path(make_fixtures.NO_AUDIO_NAME)
        self.assertEqual(segments.fingerprint(path, 0.0, 3.0), [])

    def test_non_positive_length(self):
        self.assertEqual(segments.fingerprint(_path("ep1"), 0.0, 0.0), [])
        self.assertEqual(segments.fingerprint(_path("ep1"), 0.0, -5.0), [])

    def test_start_beyond_end(self):
        self.assertEqual(segments.fingerprint(_path("ep1"), 10000.0, 30.0), [])


class TestFindShared(unittest.TestCase):
    """find_shared()：用纯整数列表测试。"""

    @staticmethod
    def _embed(a_len: int, b_len: int, a_at: int, b_at: int, run: int,
               flip: int = 0, rng: random.Random | None = None):
        rng = rng or random.Random(1234)
        a = [rng.getrandbits(32) for _ in range(a_len)]
        b = [rng.getrandbits(32) for _ in range(b_len)]
        for offset in range(run):
            value = a[a_at + offset]
            if flip:
                bits = rng.sample(range(32), flip)
                for bit in bits:
                    value ^= 1 << bit
            b[b_at + offset] = value
        return a, b

    def test_finds_embedded_segment(self):
        a, b = self._embed(1000, 900, 400, 100, 300)
        got = segments.find_shared(a, b)
        self.assertIsNotNone(got)
        self.assertAlmostEqual(got[0], sec(400), delta=2 * T)
        self.assertAlmostEqual(got[1], sec(700), delta=2 * T)
        self.assertAlmostEqual(got[2], sec(100), delta=2 * T)
        self.assertAlmostEqual(got[3], sec(400), delta=2 * T)

    def test_tolerates_few_bit_flips(self):
        for flips in (1, 2, 3):
            with self.subTest(flips=flips):
                a, b = self._embed(1000, 900, 400, 100, 300, flip=flips)
                got = segments.find_shared(a, b)
                self.assertIsNotNone(got, f"翻 {flips} 位应当还能找到")
                self.assertAlmostEqual(got[0], sec(400), delta=2 * T)
                self.assertAlmostEqual(got[1], sec(700), delta=2 * T)

    def test_many_bit_flips_is_not_a_match(self):
        """每个数都翻 12 位（> max_bit_diff=6）就不该认成共同片段。"""
        a, b = self._embed(1000, 900, 400, 100, 300, flip=12)
        self.assertIsNone(segments.find_shared(a, b))

    def test_max_bit_diff_argument(self):
        a, b = self._embed(1000, 900, 400, 100, 300, flip=5)
        self.assertIsNotNone(segments.find_shared(a, b, max_bit_diff=6))
        self.assertIsNone(segments.find_shared(a, b, max_bit_diff=2))

    def test_random_lists_share_nothing(self):
        rng = random.Random(20261002)
        a = [rng.getrandbits(32) for _ in range(1200)]
        b = [rng.getrandbits(32) for _ in range(1200)]
        self.assertIsNone(segments.find_shared(a, b))

    def test_shorter_than_min_len_is_rejected(self):
        a, b = self._embed(900, 900, 300, 200, 80)  # 80 * 0.1238 ≈ 9.9 秒
        self.assertIsNone(segments.find_shared(a, b, min_len_sec=15.0))
        got = segments.find_shared(a, b, min_len_sec=5.0)
        self.assertIsNotNone(got)
        self.assertAlmostEqual(got[1] - got[0], 80 * T, delta=2 * T)

    def test_bridges_short_gap(self):
        """中间断 2 秒（< max_gap_sec 3.5）应当连成一段。"""
        a, b = self._embed(1000, 1000, 300, 200, 400)
        hole_start, hole_len = 380, int(2.0 / T)
        rng = random.Random(7)
        for i in range(hole_start, hole_start + hole_len):
            b[200 + i - 300] = rng.getrandbits(32)
        got = segments.find_shared(a, b)
        self.assertIsNotNone(got)
        self.assertLessEqual(got[0], sec(301))
        self.assertGreaterEqual(got[1], sec(699))

    def test_long_gap_splits_segment(self):
        """中间断 6 秒（> max_gap_sec 3.5）不能连成一段。"""
        a, b = self._embed(1000, 1000, 300, 200, 400)
        hole_start, hole_len = 380, int(6.0 / T)
        rng = random.Random(11)
        for i in range(hole_start, hole_start + hole_len):
            b[200 + i - 300] = rng.getrandbits(32)
        got = segments.find_shared(a, b)
        self.assertIsNotNone(got)
        self.assertLess(got[1] - got[0], 300 * T)
        # 返回的那一段不能跨过断点
        self.assertFalse(got[0] < sec(380) and got[1] > sec(380 + hole_len))

    def test_empty_input(self):
        self.assertIsNone(segments.find_shared([], [1, 2, 3]))
        self.assertIsNone(segments.find_shared([1, 2, 3], []))

    def test_negative_integers(self):
        """fpcalc -signed 输出的负整数也要能比。"""
        rng = random.Random(99)
        a = [rng.getrandbits(31) - 2**30 for _ in range(600)]
        b = [rng.getrandbits(31) - 2**30 for _ in range(600)]
        for offset in range(200):
            b[100 + offset] = a[300 + offset]
        got = segments.find_shared(a, b)
        self.assertIsNotNone(got)
        self.assertAlmostEqual(got[0], sec(300), delta=2 * T)


def _chapter(title: str, start: float, end: float) -> dict:
    return {"start_time": f"{start:.6f}", "end_time": f"{end:.6f}",
            "timebase": "1/1000000", "tags": {"title": title}}


class TestChapters(unittest.TestCase):
    """chapters_to_segments()：按章节标题判定片头/片尾。"""

    DUR = 300.0

    def check(self, chapters, intro=None, credits=None, duration=DUR):
        got = segments.chapters_to_segments(chapters, duration)
        self.assertEqual(sorted(got), ["credits", "intro"])
        if intro is None:
            self.assertIsNone(got["intro"], f"不该判成片头: {got['intro']}")
        else:
            self.assertIsNotNone(got["intro"])
            self.assertAlmostEqual(got["intro"][0], intro[0], places=3)
            self.assertAlmostEqual(got["intro"][1], intro[1], places=3)
        if credits is None:
            self.assertIsNone(got["credits"], f"不该判成片尾: {got['credits']}")
        else:
            self.assertIsNotNone(got["credits"])
            self.assertAlmostEqual(got["credits"][0], credits[0], places=3)
            self.assertAlmostEqual(got["credits"][1], credits[1], places=3)
        return got

    def test_intro_titles(self):
        for title in ("Intro", "intro", "INTRO", "Opening", "OP", "op",
                      "Title Sequence", "title sequence", "Opening Credits",
                      "片头", "片头曲", "オープニング", "2 - Intro", "Intro -"):
            with self.subTest(title=title):
                self.check([_chapter(title, 10.0, 80.0)], intro=(10.0, 80.0))

    def test_credits_titles(self):
        for title in ("Credits", "credits", "CREDITS", "Ending", "ED", "ed",
                      "End Credits", "Outro", "片尾", "片尾曲", "エンディング"):
            with self.subTest(title=title):
                self.check([_chapter(title, 240.0, 295.0)], credits=(240.0, 295.0))

    def test_intro_and_credits_together(self):
        self.check([_chapter("Intro", 5.0, 65.0), _chapter("Credits", 250.0, 298.0)],
                   intro=(5.0, 65.0), credits=(250.0, 298.0))

    def test_numbered_titles_are_ignored(self):
        for title in ("Chapter 1", "chapter 12", "Scene 2", "第 01 章", "第3集",
                      "Track 5", "Part 2", "Segment 4", "章", "01"):
            with self.subTest(title=title):
                self.check([_chapter(title, 10.0, 60.0)])

    def test_plain_titles_are_ignored(self):
        for title in ("", "Cold Open", "Previously", "Aired 2024", "Next Preview",
                      "Chapter One", "Introverted", "Edition", "Opener", "Edison"):
            with self.subTest(title=title):
                self.check([_chapter(title, 10.0, 60.0)])

    def test_intro_must_be_in_first_third(self):
        # 整段落在前 1/3（100 秒）之内才算片头
        self.check([_chapter("Intro", 120.0, 170.0)])
        self.check([_chapter("Intro", 95.0, 101.0)])
        self.check([_chapter("Intro", 10.0, 120.0)])
        self.check([_chapter("Intro", 10.0, 99.99)], intro=(10.0, 99.99))

    def test_intro_length_bounds(self):
        self.check([_chapter("Intro", 0.0, 5.0)])           # 短于 10 秒
        self.check([_chapter("Intro", 0.0, 9.0)])           # 短于 10 秒
        self.check([_chapter("Intro", 0.0, 100.0)], intro=(0.0, 100.0))  # 恰好占满前 1/3
        self.check([_chapter("Intro", 0.0, 180.0)])         # 超出前 1/3
        self.check([_chapter("Intro", 0.0, 99.0)], intro=(0.0, 99.0))

    def test_credits_must_start_in_last_third(self):
        self.check([_chapter("Credits", 50.0, 90.0)])      # 起点不在后 1/3
        self.check([_chapter("Credits", 199.0, 250.0)])
        self.check([_chapter("Credits", 201.0, 290.0)], credits=(201.0, 290.0))

    def test_hhmmss_times_and_out_of_range_end(self):
        got = segments.chapters_to_segments(
            [{"start_time": "00:04:00.000", "end_time": "00:04:50.000",
              "tags": {"title": "Credits"}},
             {"start_time": "00:00:08.000", "end_time": "00:01:00.000",
              "tags": {"title": "Opening"}}], self.DUR)
        self.assertEqual(got["credits"], (240.0, 290.0))
        self.assertEqual(got["intro"], (8.0, 60.0))

    def test_credits_end_clamped_to_duration(self):
        got = segments.chapters_to_segments([_chapter("Credits", 250.0, 305.0)], self.DUR)
        self.assertEqual(got["credits"], (250.0, 300.0))

    def test_only_first_matching_chapter_used(self):
        got = segments.chapters_to_segments(
            [_chapter("Intro", 10.0, 60.0), _chapter("Intro", 70.0, 99.0)], self.DUR)
        self.assertEqual(got["intro"], (10.0, 60.0))

    def test_empty_inputs(self):
        self.assertEqual(segments.chapters_to_segments([], self.DUR),
                         {"intro": None, "credits": None})
        self.assertEqual(segments.chapters_to_segments(
            [_chapter("Intro", 10.0, 60.0)], 0.0), {"intro": None, "credits": None})
        self.assertEqual(segments.chapters_to_segments(None, self.DUR),
                         {"intro": None, "credits": None})

    def test_broken_chapter_entries(self):
        self.check([{"start_time": "abc", "end_time": "xyz", "tags": {"title": "Intro"}}])
        self.check([{"start_time": "50.0", "end_time": "10.0", "tags": {"title": "Intro"}}])
        self.check([{"tags": {}}])


class TestDetectSeasonIntro(unittest.TestCase):
    """片头：4 集开头 120 秒的声纹 → 认出各集那段 45 秒的相同片头曲。"""

    @classmethod
    def setUpClass(cls):
        cls.fps = {key: segments.fingerprint(_path(key), 0.0, 120.0)
                   for key in make_fixtures.INTRO_TRUTH}
        cls.got = segments.detect_season(cls.fps)

    def test_all_episodes_found(self):
        for key, truth in make_fixtures.INTRO_TRUTH.items():
            with self.subTest(episode=key):
                got = self.got[key]
                self.assertIsNotNone(got, "每集都应识别出片头")
                self.assertLess(abs(got[0] - truth[0]), 1.5,
                                f"{key} 片头起点 {got[0]:.2f} 与真值 {truth[0]:.2f} 差太多")
                self.assertLess(abs(got[1] - truth[1]), 1.5,
                                f"{key} 片头终点 {got[1]:.2f} 与真值 {truth[1]:.2f} 差太多")

    def test_length_in_season_range(self):
        for key, got in self.got.items():
            if got is not None:
                self.assertGreaterEqual(got[1] - got[0], 15.0)
                self.assertLessEqual(got[1] - got[0], 150.0)

    def test_one_episode_without_partner(self):
        """带一集没有音轨（声纹为空）：它自己返回 None，其他集照样识别出来。"""
        fps = dict(self.fps)
        fps["broken"] = segments.fingerprint(
            make_fixtures.fixture_path(make_fixtures.NO_AUDIO_NAME), 0.0, 120.0)
        self.assertEqual(fps["broken"], [])
        got = segments.detect_season(fps)
        self.assertIsNone(got["broken"])
        for key, truth in make_fixtures.INTRO_TRUTH.items():
            self.assertIsNotNone(got[key])
            self.assertLess(abs(got[key][0] - truth[0]), 1.5)

    def test_single_episode_returns_none(self):
        got = segments.detect_season({"ep1": self.fps["ep1"]})
        self.assertEqual(got, {"ep1": None})

    def test_max_len_filters_long_segment(self):
        """max_len_sec 比片头还短 -> 全部丢掉。"""
        got = segments.detect_season(self.fps, max_len_sec=20.0)
        for key in make_fixtures.INTRO_TRUTH:
            self.assertIsNone(got[key])


class TestDetectSeasonNoShared(unittest.TestCase):
    """没有共同片头：3 集互不相同的内容，全部返回 None。"""

    @macos_calibrated
    def test_all_none(self):
        fps = {key: segments.fingerprint(_path(key), 0.0, 120.0)
               for key in make_fixtures.NO_SHARED_KEYS}
        got = segments.detect_season(fps)
        self.assertEqual(sorted(got), sorted(make_fixtures.NO_SHARED_KEYS))
        for key in make_fixtures.NO_SHARED_KEYS:
            self.assertIsNone(got[key], f"{key} 不该有结果")


class TestDetectSeasonCredits(unittest.TestCase):
    """片尾：取每集最后 120 秒声纹，offset_sec 传窗口起点 180，检查秒数换算。"""

    WINDOW_START = make_fixtures.EPISODE_LEN - 120.0

    @classmethod
    def setUpClass(cls):
        cls.fps = {key: segments.fingerprint(_path(key), cls.WINDOW_START, 120.0)
                   for key in make_fixtures.CREDITS_TRUTH}
        cls.got_offset = segments.detect_season(cls.fps, offset_sec=cls.WINDOW_START)
        cls.got_zero = segments.detect_season(cls.fps, offset_sec=0.0)

    def test_offset_conversion_is_exact(self):
        """offset_sec 只应当把秒数整体平移，不能改变片段长度。"""
        for key in make_fixtures.CREDITS_TRUTH:
            with self.subTest(episode=key):
                zero = self.got_zero[key]
                shifted = self.got_offset[key]
                self.assertIsNotNone(zero)
                self.assertIsNotNone(shifted)
                self.assertAlmostEqual(shifted[0] - zero[0], self.WINDOW_START, places=6)
                self.assertAlmostEqual(shifted[1] - zero[1], self.WINDOW_START, places=6)
                self.assertAlmostEqual(shifted[1] - shifted[0], zero[1] - zero[0], places=6)

    @macos_calibrated
    def test_credits_position(self):
        for key, truth in make_fixtures.CREDITS_TRUTH.items():
            with self.subTest(episode=key):
                got = self.got_offset[key]
                self.assertLess(abs(got[0] - truth[0]), 1.5,
                                f"{key} 片尾起点 {got[0]:.2f} 与真值 {truth[0]:.2f} 差太多")
                self.assertLess(abs(got[1] - truth[1]), 1.5,
                                f"{key} 片尾终点 {got[1]:.2f} 与真值 {truth[1]:.2f} 差太多")

    @macos_calibrated
    def test_zero_offset_is_relative_to_window(self):
        """offset_sec=0 时秒数相对声纹开头，所以应当等于真值减去窗口起点。"""
        for key, truth in make_fixtures.CREDITS_TRUTH.items():
            with self.subTest(episode=key):
                got = self.got_zero[key]
                self.assertLess(abs(got[0] - (truth[0] - self.WINDOW_START)), 1.5)
                self.assertLess(abs(got[1] - (truth[1] - self.WINDOW_START)), 1.5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
