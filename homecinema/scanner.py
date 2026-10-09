"""扫描本地影片目录，按约定结构解析出电影与剧集，增量写入库。

电影：`<movie_root>/<Title> (<Year>)/<Title> (<Year>).<ext>`，一个文件夹一部电影
（同文件夹多个视频取文件名序第一个）。
剧集：`<tv_root>/<Series>[ (Year)]/Season NN/<Series> SxxExx.<ext>`；
    没有 `Season NN` 层时文件直接在 `<Series>/` 下。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .db import Library, utcnow

VIDEO_EXTS = {"mp4", "mkv", "avi", "rmvb", "m4v", "mov", "ts", "wmv", "flv", "webm"}

# 年份括号也认全角（中文输入法默认）：`电影（2001）`
FOLDER_TITLE_YEAR = re.compile(r"^(.*?)[ \t_]*[(（](\d{4})[)）][ \t]*$")
# `Season 1`、`Season_01`、`Season 1 (2020)`、`第1季`；`Specials` 按 Plex/Jellyfin 惯例当第 0 季
SEASON_DIR = re.compile(r"^(?:Season[ _-]*(\d{1,2})(?:[ _-]+.*)?|第\s*(\d{1,2})\s*季|(Specials?))\s*$",
                        re.IGNORECASE)
# 双集文件 "S06E01E02" 也要认：取第一集的集号。
# 前面不用 \b：`名_S01E01`、`剧名S01E01` 里下划线和汉字都算词字符，\b 不成立；改成「前面不是字母数字」。
# S 与 E 之间允许空格、点、横线、下划线（`S01.E01`）。
EPISODE_TAG = re.compile(r"(?i)(?<![a-z0-9])s(\d{1,2})[ ._-]?e(\d{1,3})(?!\d)")
# 后备：`1x01` 写法（前后不能紧邻数字，免得把 1920x1080 认成集号）
EPISODE_TAG_X = re.compile(r"(?i)(?<![a-z0-9])(\d{1,2})x(\d{2,3})(?!\d)")


def season_from_dir(name: str) -> int | None:
    """季目录名 → 季号；不是季目录返回 None。"""
    m = SEASON_DIR.match(name.strip())
    if not m:
        return None
    if m.group(3):
        return 0
    return int(m.group(1) or m.group(2))


def episode_tag(name: str) -> tuple[int, int] | None:
    """文件名里的 (季, 集)；没有返回 None。"""
    m = EPISODE_TAG.search(name) or EPISODE_TAG_X.search(name)
    return (int(m.group(1)), int(m.group(2))) if m else None


def is_video_file(name: str) -> bool:
    """视频扩展名大小写不敏感；忽略隐藏文件、._* 与 .DS_Store。"""
    if name.startswith("."):
        return False
    return name.rsplit(".", 1)[-1].lower() in VIDEO_EXTS


def parse_title_year(name: str) -> tuple[str, int | None]:
    """`Example Movie (2001)` → ("Example Movie", 2001)；无年份则整名当标题。"""
    m = FOLDER_TITLE_YEAR.match(name.strip())
    if m:
        title = m.group(1).strip()
        if title:
            return title, int(m.group(2))
    return name.strip(), None


@dataclass
class MovieFile:
    path: str
    folder: str
    video_name: str
    ext: str
    title: str
    year: int | None
    size: int
    mtime: float
    added_at: str = ""


@dataclass
class EpisodeFile:
    path: str
    show_path: str
    show_title: str
    show_year: int | None
    season_number: int
    episode_number: int
    video_name: str
    ext: str
    title: str
    size: int
    mtime: float
    added_at: str = ""


@dataclass
class ScanResult:
    movies: list[MovieFile] = field(default_factory=list)
    episodes: list[EpisodeFile] = field(default_factory=list)
    started_at: str = ""  # Kimi-D-3：扫描开始时刻，标 missing 时用来放过并发写入


def _file_stat(path: Path) -> tuple[int, float, str]:
    """大小、mtime、入库时间（文件创建时间，取不到用 mtime）。

    「最近添加」按文件真正进片库的时间排，而不是第一次扫描的时间——
    否则首次扫描时几千个文件的 added_at 都一样。"""
    st = path.stat()
    born = min(getattr(st, "st_birthtime", st.st_mtime) or st.st_mtime, st.st_mtime)
    added = datetime.fromtimestamp(born, timezone.utc).isoformat(timespec="microseconds")
    return st.st_size, st.st_mtime, added


def scan_movie_root(root: str, result: ScanResult) -> None:
    base = Path(root).expanduser()
    if not base.is_dir():
        return
    for entry in sorted(base.iterdir(), key=lambda p: p.name):
        if entry.name.startswith(".") or not entry.is_dir():
            continue
        try:
            videos = [p for p in sorted(entry.iterdir(), key=lambda p: p.name)
                      if p.is_file() and is_video_file(p.name)]
            if not videos:
                continue
            video = videos[0]  # 约定一个文件夹一部电影；多个时取文件名序第一个
            size, mtime, added = _file_stat(video)
        except OSError:
            continue  # Kimi-D-2：单个文件夹被移走/暂时不可读，跳过它，不中断整轮扫描
        title, year = parse_title_year(entry.name)
        result.movies.append(MovieFile(
            path=str(video.resolve()), folder=str(entry.resolve()),
            video_name=video.name, ext=video.suffix.lstrip(".").lower(),
            title=title, year=year, size=size, mtime=mtime, added_at=added))


def scan_tv_root(root: str, result: ScanResult) -> None:
    base = Path(root).expanduser()
    if not base.is_dir():
        return
    for series_dir in sorted(base.iterdir(), key=lambda p: p.name):
        if series_dir.name.startswith(".") or not series_dir.is_dir():
            continue
        try:
            # Season NN 层
            season_dirs = [p for p in sorted(series_dir.iterdir(), key=lambda p: p.name)
                           if p.is_dir() and not p.name.startswith(".")]
        except OSError:
            continue  # Kimi-D-2：单部剧集目录暂时不可读，跳过它，不中断整轮扫描
        show_title, show_year = parse_title_year(series_dir.name)
        for sub in season_dirs:
            sn = season_from_dir(sub.name)
            if sn is not None:
                _scan_episode_dir(sub, series_dir, show_title, show_year, sn, result)
        # 直接放在剧集文件夹下的视频（无 Season 层），季号从文件名 SxxExx 取
        _scan_episode_dir(series_dir, series_dir, show_title, show_year, None, result,
                          nested=False)


def _scan_episode_dir(dir_path: Path, series_dir: Path, show_title: str,
                      show_year: int | None, season_number: int | None,
                      result: ScanResult, nested: bool = True) -> None:
    try:
        files = sorted(dir_path.iterdir(), key=lambda p: p.name)
    except OSError:
        return  # Kimi-D-2：目录暂时不可读，整目录跳过
    for f in files:
        if not f.is_file() or not is_video_file(f.name):
            continue
        tag = episode_tag(f.name)
        if not tag:
            continue  # 文件名里没有 SxxExx / 1x01 的不入库
        season = tag[0] if season_number is None else season_number
        episode = tag[1]
        try:
            size, mtime, added = _file_stat(f)
        except OSError:
            continue  # Kimi-D-2：文件在列举与 stat 之间被移走，跳过这一集
        result.episodes.append(EpisodeFile(
            path=str(f.resolve()), show_path=str(series_dir.resolve()),
            show_title=show_title, show_year=show_year,
            season_number=season, episode_number=episode,
            video_name=f.name, ext=f.suffix.lstrip(".").lower(),
            title=f.stem, size=size, mtime=mtime, added_at=added))


def scan_paths(config: Config) -> ScanResult:
    result = ScanResult()
    for root in config.movie_roots:
        scan_movie_root(root, result)
    for root in config.tv_roots:
        scan_tv_root(root, result)
    return result


def apply_scan(db: Library, result: ScanResult) -> dict:
    """增量入库：新增/变化 upsert（保住 added_at），消失标 missing（不删记录）。"""
    seen_movies = {m.path for m in result.movies}
    seen_episodes = {e.path for e in result.episodes}
    for m in result.movies:
        db.upsert_movie(path=m.path, folder=m.folder, video_name=m.video_name,
                        ext=m.ext, title=m.title, year=m.year, size=m.size, mtime=m.mtime,
                        added_at=m.added_at or None)
    show_ids: dict[str, int] = {}
    for e in result.episodes:
        sid = show_ids.get(e.show_path)
        if sid is None:
            sid = db.upsert_show(path=e.show_path, folder=e.show_path,
                                 title=e.show_title, year=e.show_year)
            show_ids[e.show_path] = sid
        db.upsert_episode(show_id=sid, season_number=e.season_number,
                          episode_number=e.episode_number, path=e.path,
                          video_name=e.video_name, ext=e.ext, title=e.title,
                          size=e.size, mtime=e.mtime, added_at=e.added_at or None)
    missing_movies = db.mark_missing_movies(seen_movies,
                                            before=result.started_at or None)
    missing_episodes = db.mark_missing_episodes(seen_episodes,
                                                before=result.started_at or None)
    db.refresh_show_missing()
    db.refresh_show_added()
    return {
        "movies": len(result.movies),
        "episodes": len(result.episodes),
        "missing_movies": missing_movies,
        "missing_episodes": missing_episodes,
    }


def run_scan(config: Config, db: Library, progress=None) -> dict:
    """扫描 + 入库。progress(phase, done, total) 用于 /api/scan/status。"""
    if progress:
        progress("scanning", 0, 0)
    started = utcnow()  # Kimi-D-3：扫描开始时刻，期间被其他进程 upsert 的路径不误标 missing
    roots = [Path(r).expanduser() for r in (*config.movie_roots, *config.tv_roots)]
    present = [r for r in roots if r.is_dir()]
    result = scan_paths(config)
    # Kimi-D-2 验收补：子目录出错改为跳过后，扫描途中外接盘卸载会让每个子目录都被跳过、
    # 随后整库标 missing。开始时在、扫完不在的根目录 → 整轮中止，不入库不标 missing。
    gone = [str(r) for r in present if not r.is_dir()]
    if gone:
        raise OSError(f"扫描途中片库根目录不可用：{', '.join(gone)}")
    result.started_at = started
    stats = apply_scan(db, result)
    if progress:
        progress("scanning", stats["movies"] + stats["episodes"],
                 stats["movies"] + stats["episodes"])
    return stats
