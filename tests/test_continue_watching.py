"""「继续观看」与下一集逻辑：看完 S01E02 推 S01E03；季末推下季；全看完不出现。"""

from __future__ import annotations

from homecinema.db import Library


def seed_show(lib: Library) -> tuple[int, dict]:
    show_id = lib.upsert_show(path="/lib-fixture/Sample Show", folder="/lib-fixture/Sample Show",
                              title="Sample Show", year=2001)
    eps = {}
    for season, episode in [(1, 1), (1, 2), (1, 3), (2, 1)]:
        eps[(season, episode)] = lib.upsert_episode(
            show_id=show_id, season_number=season, episode_number=episode,
            path=f"/lib-fixture/Sample Show/S{season:02d}E{episode:02d}.mkv",
            video_name=f"S{season:02d}E{episode:02d}.mkv", ext="mkv",
            title=f"Sample Show S{season:02d}E{episode:02d}", size=1, mtime=1.0)
    return show_id, eps


def seed_movie(lib: Library, title="Example Movie", path="/lib-fixture/Example Movie (2001).mp4"):
    return lib.upsert_movie(path=path, folder="/lib-fixture", video_name=path.rsplit("/", 1)[-1],
                            ext="mp4", title=title, year=2001, size=1, mtime=1.0)


def test_next_episode_within_season(lib):
    show_id, eps = seed_show(lib)
    lib.save_playback("episode", eps[(1, 1)], 1296, 1300, True)
    lib.save_playback("episode", eps[(1, 2)], 1296, 1300, True)
    ep, source = lib.next_up_for_show(show_id)
    assert source == "next"
    assert (ep["season_number"], ep["episode_number"]) == (1, 3)


def test_next_episode_across_season(lib):
    show_id, eps = seed_show(lib)
    for key in [(1, 1), (1, 2), (1, 3)]:
        lib.save_playback("episode", eps[key], 1296, 1300, True)
    ep, source = lib.next_up_for_show(show_id)
    assert source == "next"
    assert (ep["season_number"], ep["episode_number"]) == (2, 1)  # 季末推下季第一集


def test_all_watched_disappears_from_continue_watching(lib):
    show_id, eps = seed_show(lib)
    for key in [(1, 1), (1, 2), (1, 3), (2, 1)]:
        lib.save_playback("episode", eps[key], 1296, 1300, True)
    assert lib.next_up_for_show(show_id) is None
    cards = lib.continue_watching()
    assert all(c["show_id"] != show_id for c in cards)


def test_in_progress_episode_resumes(lib):
    show_id, eps = seed_show(lib)
    lib.save_playback("episode", eps[(1, 1)], 1296, 1300, True)
    lib.save_playback("episode", eps[(1, 2)], 600, 1300, False)  # E02 看到一半
    ep, source = lib.next_up_for_show(show_id)
    assert source == "resume"
    assert (ep["season_number"], ep["episode_number"]) == (1, 2)


def test_never_played_show_not_in_continue_watching(lib):
    seed_show(lib)
    cards = lib.continue_watching()
    assert cards == []  # 没有播放记录的剧不出现在「继续观看」
    ep, source = lib.next_up_for_show(lib.list_shows()[0]["id"])
    assert source == "first"  # 详情页仍给出第一集


def test_episode_missing_skipped_in_next(lib):
    show_id, eps = seed_show(lib)
    lib.save_playback("episode", eps[(1, 1)], 1296, 1300, True)
    lib._conn.execute("UPDATE episodes SET missing=1 WHERE season_number=1 AND episode_number=2")
    lib._conn.commit()
    ep, _source = lib.next_up_for_show(show_id)
    assert (ep["season_number"], ep["episode_number"]) == (1, 3)  # 跳过缺失的 E02


def test_movie_continue_watching_rules(lib):
    movie_id = seed_movie(lib)
    # 没进度 → 不出现
    assert lib.continue_watching() == []
    # <30 秒 → 当没看过
    lib.save_playback("movie", movie_id, 10, 1200, False)
    assert lib.continue_watching() == []
    # 有进度未看完 → 出现
    lib.save_playback("movie", movie_id, 600, 1200, False)
    cards = lib.continue_watching()
    assert len(cards) == 1
    card = cards[0]
    assert card["type"] == "movie" and card["id"] == movie_id
    assert abs(card["progress"] - 0.5) < 1e-6
    assert card["href"] == f"#/movie/{movie_id}"
    # 看完 → 不出现
    lib.save_playback("movie", movie_id, 1150, 1200, True)
    assert lib.continue_watching() == []


def test_continue_watching_sorted_by_recent(lib):
    import time as _time
    movie1 = seed_movie(lib, title="Example Movie", path="/lib-fixture/m1.mp4")
    movie2 = seed_movie(lib, title="Another Film", path="/lib-fixture/m2.mp4")
    show_id, eps = seed_show(lib)
    lib.save_playback("movie", movie1, 600, 1200, False)
    _time.sleep(0.01)
    lib.save_playback("episode", eps[(1, 1)], 600, 1300, False)
    _time.sleep(0.01)
    lib.save_playback("movie", movie2, 300, 1200, False)
    cards = lib.continue_watching(limit=20)
    assert [c["id"] for c in cards] == [movie2, eps[(1, 1)], movie1]  # 最近播放在前

    limited = lib.continue_watching(limit=2)
    assert len(limited) == 2 and limited[0]["id"] == movie2


def test_recently_added(lib):
    import time as _time
    movie_id = seed_movie(lib)
    _time.sleep(0.01)
    show_id, eps = seed_show(lib)
    recent = lib.recently_added(10)
    # 剧集一部一张卡，不按单集刷屏；后入库的排前面
    assert [r["type"] for r in recent] == ["show", "movie"]
    assert recent[0]["href"] == f"#/show/{show_id}"
    assert any(r["type"] == "movie" and r["id"] == movie_id for r in recent)
