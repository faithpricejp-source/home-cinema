"""扫描器测试：tmp_path 造假目录树（虚构名，空文件）。"""

from __future__ import annotations

from homecinema.scanner import run_scan


def make_movie(root, folder_name, ext="mp4"):
    folder = root / folder_name
    folder.mkdir(parents=True)
    video = folder / f"{folder_name}.{ext}"
    video.write_bytes(b"")
    return folder, video


def make_episode(series_dir, season_dir, filename):
    target = series_dir / season_dir if season_dir else series_dir
    target.mkdir(parents=True, exist_ok=True)
    f = target / filename
    f.write_bytes(b"")
    return f


def test_movie_scan_parses_title_year_and_ext(tmp_path, cfg, lib):
    movies = tmp_path / "movies"
    make_movie(movies, "Example Movie (2001)", "mp4")
    make_movie(movies, "Another Film (1999)", "MKV")  # 大写扩展名
    (movies / "Example Movie (2001)" / ".DS_Store").write_bytes(b"")
    (movies / "Example Movie (2001)" / "._junk.mp4").write_bytes(b"")

    stats = run_scan(cfg, lib)
    assert stats["movies"] == 2

    rows = lib.list_movies(sort="title")
    by_title = {r["title"]: r for r in rows}
    assert set(by_title) == {"Example Movie", "Another Film"}
    assert by_title["Example Movie"]["year"] == 2001
    assert by_title["Another Film"]["ext"] == "mkv"
    assert by_title["Example Movie"]["path"].endswith("Example Movie (2001).mp4")


def test_movie_without_year_and_hidden_only_folder(tmp_path, cfg, lib):
    movies = tmp_path / "movies"
    make_movie(movies, "No Year Movie", "mkv")
    hidden = movies / ".Hidden Film"
    hidden.mkdir()
    (hidden / ".Hidden Film.mp4").write_bytes(b"")  # 隐藏文件夹整个跳过
    empty = movies / "Empty Folder (2020)"
    empty.mkdir()  # 无视频文件的文件夹不入库

    run_scan(cfg, lib)
    rows = lib.list_movies()
    assert [r["title"] for r in rows] == ["No Year Movie"]
    assert rows[0]["year"] is None


def test_tv_scan_season_layer_and_year(tmp_path, cfg, lib):
    tv = tmp_path / "tv"
    show = tv / "Sample Show"
    make_episode(show, "Season 01", "Sample Show S01E01.mkv")
    make_episode(show, "Season 01", "Sample Show S01E02.MP4")
    old = tv / "Old Show (1998)"
    make_episode(old, "Season 2", "Old Show S02E01.webm")
    other = tv / "Other Series"
    make_episode(other, None, "Other Series S01E01.avi")  # 无 Season 层
    make_episode(other, None, "Sample Notes.txt")  # 非视频
    make_episode(other, None, "Trailer.mkv")  # 视频但没有 SxxExx → 跳过

    run_scan(cfg, lib)
    shows = {r["title"]: r for r in lib.list_shows()}
    assert set(shows) == {"Sample Show", "Old Show", "Other Series"}
    assert shows["Old Show"]["year"] == 1998

    sample_eps = lib.episodes_for_show(shows["Sample Show"]["id"])
    assert [(e["season_number"], e["episode_number"]) for e in sample_eps] == [(1, 1), (1, 2)]
    assert sample_eps[0]["ext"] == "mkv" and sample_eps[1]["ext"] == "mp4"

    other_eps = lib.episodes_for_show(shows["Other Series"]["id"])
    assert [(e["season_number"], e["episode_number"]) for e in other_eps] == [(1, 1)]
    assert lib.episodes_for_show(shows["Old Show"]["id"])[0]["season_number"] == 2


def test_missing_marked_and_rescan_no_duplicates(tmp_path, cfg, lib):
    movies = tmp_path / "movies"
    folder_a, _ = make_movie(movies, "Example Movie (2001)")
    make_movie(movies, "Another Film (1999)")
    run_scan(cfg, lib)
    rows = lib.list_movies()
    assert len(rows) == 2
    added_at = {r["title"]: r["added_at"] for r in rows}

    lib.save_playback("movie", rows[0]["id"], 600, 1200, False)

    import shutil
    shutil.rmtree(folder_a)  # Example Movie 消失
    run_scan(cfg, lib)

    rows2 = lib.list_movies()
    assert len(rows2) == 1  # 列表只显示在架的
    assert rows2[0]["title"] == "Another Film"

    from homecinema.db import Library
    with_rows = lib._conn.execute("SELECT title, missing, added_at FROM movies").fetchall()
    by_title = {r["title"]: r for r in with_rows}
    assert by_title["Example Movie"]["missing"] == 1  # 消失只标 missing，不删记录
    assert by_title["Example Movie"]["added_at"] == added_at["Example Movie"]
    pb = lib.get_playback("movie", rows[0]["id"])
    assert pb is not None and pb["position_sec"] == 600  # 进度保住

    run_scan(cfg, lib)  # 重扫不产生重复、不再变化
    all_rows = lib._conn.execute("SELECT COUNT(*) AS n FROM movies").fetchone()
    assert all_rows["n"] == 2


def test_episode_missing_marks_show_missing(tmp_path, cfg, lib):
    tv = tmp_path / "tv"
    show = tv / "Sample Show"
    ep1 = make_episode(show, "Season 01", "Sample Show S01E01.mkv")
    run_scan(cfg, lib)
    shows = lib.list_shows()
    assert shows[0]["missing"] == 0

    ep1.unlink()
    run_scan(cfg, lib)
    shows = lib.list_shows()
    assert shows == []  # 全部剧集消失 → show 标 missing，列表隐藏

    show_rows = lib._conn.execute("SELECT missing FROM shows").fetchall()
    assert show_rows[0]["missing"] == 1


def test_video_ext_case_insensitive_and_hidden_ignored(tmp_path, cfg, lib):
    movies = tmp_path / "movies"
    folder, _ = make_movie(movies, "Mixed Case (2010)", "RmVb")
    run_scan(cfg, lib)
    rows = lib.list_movies()
    assert len(rows) == 1 and rows[0]["ext"] == "rmvb"

    # 只有隐藏视频的文件夹不入库
    only_hidden = movies / "Only Hidden (2011)"
    only_hidden.mkdir()
    (only_hidden / ".hidden.mp4").write_bytes(b"")
    (only_hidden / "._apple.mp4").write_bytes(b"")
    run_scan(cfg, lib)
    titles = [r["title"] for r in lib.list_movies()]
    assert "Only Hidden" not in titles
