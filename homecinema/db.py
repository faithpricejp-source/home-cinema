"""SQLite 存储层。所有读写经单个连接 + RLock，供后台扫描线程与请求线程共用。

约定：SQL 一律是 execute() 调用处的内联单行字面量 + 参数占位符，不做任何拼接。
"""

from __future__ import annotations

import functools
import json
import re
import sqlite3
import threading
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS movies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL UNIQUE,
    folder TEXT NOT NULL,
    video_name TEXT NOT NULL,
    ext TEXT NOT NULL,
    title TEXT NOT NULL,
    year INTEGER,
    size INTEGER NOT NULL DEFAULT 0,
    mtime REAL NOT NULL DEFAULT 0,
    added_at TEXT NOT NULL,
    updated_at TEXT,
    missing INTEGER NOT NULL DEFAULT 0,
    tmdb_id INTEGER,
    overview TEXT NOT NULL DEFAULT '',
    genres TEXT NOT NULL DEFAULT '[]',
    rating REAL,
    runtime_minutes INTEGER,
    poster_path TEXT,
    backdrop_path TEXT,
    poster_local TEXT,
    poster_cached TEXT,
    backdrop_cached TEXT,
    status TEXT NOT NULL DEFAULT 'unmatched',
    metadata_at TEXT
);
CREATE TABLE IF NOT EXISTS shows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL UNIQUE,
    folder TEXT NOT NULL,
    title TEXT NOT NULL,
    year INTEGER,
    added_at TEXT NOT NULL,
    updated_at TEXT,
    missing INTEGER NOT NULL DEFAULT 0,
    tmdb_id INTEGER,
    overview TEXT NOT NULL DEFAULT '',
    genres TEXT NOT NULL DEFAULT '[]',
    rating REAL,
    runtime_minutes INTEGER,
    poster_path TEXT,
    backdrop_path TEXT,
    poster_local TEXT,
    poster_cached TEXT,
    backdrop_cached TEXT,
    status TEXT NOT NULL DEFAULT 'unmatched',
    metadata_at TEXT
);
CREATE TABLE IF NOT EXISTS seasons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    show_id INTEGER NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
    season_number INTEGER NOT NULL,
    name TEXT,
    episode_count INTEGER,
    poster_path TEXT,
    UNIQUE(show_id, season_number)
);
CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    show_id INTEGER NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
    season_number INTEGER NOT NULL,
    episode_number INTEGER NOT NULL,
    path TEXT NOT NULL UNIQUE,
    video_name TEXT NOT NULL,
    ext TEXT NOT NULL,
    title TEXT NOT NULL,
    size INTEGER NOT NULL DEFAULT 0,
    mtime REAL NOT NULL DEFAULT 0,
    added_at TEXT NOT NULL,
    updated_at TEXT,
    missing INTEGER NOT NULL DEFAULT 0,
    tmdb_id INTEGER,
    overview TEXT NOT NULL DEFAULT '',
    still_path TEXT,
    still_cached TEXT,
    runtime_minutes INTEGER,
    air_date TEXT,
    metadata_at TEXT
);
CREATE TABLE IF NOT EXISTS playback (
    item_type TEXT NOT NULL CHECK (item_type IN ('movie', 'episode')),
    item_id INTEGER NOT NULL,
    position_sec REAL NOT NULL DEFAULT 0,
    duration_sec REAL,
    watched INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT,
    PRIMARY KEY (item_type, item_id)
);
CREATE TABLE IF NOT EXISTS segments (
    episode_id INTEGER PRIMARY KEY REFERENCES episodes(id) ON DELETE CASCADE,
    intro_start REAL, intro_end REAL,
    credits_start REAL, credits_end REAL,
    source TEXT NOT NULL,
    detected_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recommendations (
    kind TEXT NOT NULL CHECK (kind IN ('movie','tv')),
    tmdb_id INTEGER NOT NULL,
    title TEXT NOT NULL, original_title TEXT, year INTEGER, overview TEXT,
    poster_cached TEXT, vote_average REAL, vote_count INTEGER,
    score REAL NOT NULL, because TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    PRIMARY KEY (kind, tmdb_id)
);
CREATE TABLE IF NOT EXISTS rec_dismissed (
    kind TEXT NOT NULL, tmdb_id INTEGER NOT NULL, dismissed_at TEXT NOT NULL,
    PRIMARY KEY (kind, tmdb_id)
);
CREATE TABLE IF NOT EXISTS community_segments (
    episode_id INTEGER PRIMARY KEY REFERENCES episodes(id) ON DELETE CASCADE,
    provider TEXT, status TEXT NOT NULL,
    intro_start REAL, intro_end REAL, recap_start REAL, recap_end REAL,
    credits_start REAL, credits_end REAL, preview_start REAL,
    fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS people (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL, profile_path TEXT, profile_cached TEXT
);
CREATE TABLE IF NOT EXISTS credits (
    item_type TEXT NOT NULL CHECK (item_type IN ('movie','show')),
    item_id INTEGER NOT NULL,
    person_id INTEGER NOT NULL REFERENCES people(id),
    role TEXT NOT NULL CHECK (role IN ('director','creator','cast')),
    character TEXT, ord INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (item_type, item_id, person_id, role)
);
CREATE TABLE IF NOT EXISTS collections (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL, overview TEXT NOT NULL DEFAULT '', poster_cached TEXT
);
CREATE TABLE IF NOT EXISTS favorites (
    item_type TEXT NOT NULL CHECK (item_type IN ('movie','show')),
    item_id INTEGER NOT NULL, added_at TEXT NOT NULL,
    PRIMARY KEY (item_type, item_id)
);
CREATE TABLE IF NOT EXISTS subtitles (
    item_type TEXT NOT NULL CHECK (item_type IN ('movie','episode')),
    item_id INTEGER NOT NULL, path TEXT NOT NULL, sub_id INTEGER, added_at TEXT NOT NULL,
    PRIMARY KEY (item_type, item_id, path)
);
CREATE INDEX IF NOT EXISTS idx_episodes_show ON episodes(show_id, season_number, episode_number);
CREATE INDEX IF NOT EXISTS idx_credits_person ON credits(person_id);
CREATE INDEX IF NOT EXISTS idx_playback_updated ON playback(updated_at);
"""

# 后加的列：老库启动时补（SQLite 的 CREATE TABLE IF NOT EXISTS 不会给已有表加列）
_ADDED_COLUMNS = (
    ("movies", "collection_id", "INTEGER"),
    ("movies", "credits_at", "TEXT"),
    ("movies", "video_height", "INTEGER"),
    ("shows", "imdb_id", "TEXT"),
    ("shows", "credits_at", "TEXT"),
    ("shows", "tmdb_status", "TEXT"),
    ("shows", "next_air", "TEXT"),
    ("shows", "last_air", "TEXT"),
    ("shows", "airing_at", "TEXT"),
    ("collections", "parts", "TEXT"),
    ("movies", "aka", "TEXT"),
    ("shows", "aka", "TEXT"),
    ("recommendations", "fit", "REAL"),
    ("people", "aka", "TEXT"),      # 10-07：英文常用名＋TMDB 别名（原名、中文译名等），供搜索
    ("people", "aka_at", "TEXT"),
    ("people", "aka_zh", "TEXT"),   # 10-07：Wikidata 中文标签/别名（tools/import_people_zh.py 导入，与 TMDB 别名分开存，重拉不冲掉）
)

_MOVIE_META_COLS = ("tmdb_id", "overview", "genres", "rating", "runtime_minutes",
                    "poster_path", "backdrop_path", "poster_local", "poster_cached",
                    "backdrop_cached", "status")
_SHOW_META_COLS = _MOVIE_META_COLS
_EPISODE_META_COLS = ("tmdb_id", "overview", "still_path", "still_cached",
                      "runtime_minutes", "air_date", "title")


@functools.lru_cache(maxsize=1)
def _t2s():
    """繁转简（opencc-python-reimplemented）；没装就不转，搜索照常工作。"""
    try:
        from opencc import OpenCC
        return OpenCC("t2s").convert
    except Exception:
        return lambda t: t


@functools.lru_cache(maxsize=65536)
def _search_key(text: str | None) -> str:
    """搜索比对用：繁转简、全角转半角、忽略大小写、去掉空格和标点
    （「你丫闭嘴」能搜到「你丫闭嘴！」，「洪金寶」能搜到「洪金宝」）。"""
    t = _t2s()(unicodedata.normalize("NFKC", text or "")).casefold()
    return "".join(ch for ch in t if ch.isalnum())


def _title_hit(row, key: str, people_hits: set[int] = frozenset()) -> bool:
    """q 命中英文片名、aka 里的中文译名/原名，或（people_hits 给出的）演员/导演名、角色名。"""
    return (key in _search_key(row["title"]) or key in _search_key(row["aka"])
            or row["id"] in people_hits)


def utcnow() -> str:
    """UTC ISO 时间戳，微秒精度（updated_at 用于排序，需区分同秒内的写入）。"""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class Library:
    def __init__(self, db_path: str | Path):
        self.path = Path(db_path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        # timeout + WAL：补元数据的命令行进程和服务同时写库时，等锁而不是直接报 locked
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._lock:
            self._conn.executescript(SCHEMA)
            for table, column, kind in _ADDED_COLUMNS:
                have = {r["name"] for r in self._conn.execute("SELECT name FROM pragma_table_info(?)", (table,)).fetchall()}
                if column not in have:
                    # 表名/列名/类型来自上面的常量表，不是外部输入
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------- movies ----------

    def upsert_movie(self, *, path: str, folder: str, video_name: str, ext: str,
                     title: str, year: int | None, size: int, mtime: float,
                     added_at: str | None = None) -> int:
        now = utcnow()
        with self._lock:
            self._conn.execute("INSERT INTO movies (path, folder, video_name, ext, title, year, size, mtime, added_at, updated_at, missing) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0) ON CONFLICT(path) DO UPDATE SET folder=excluded.folder, video_name=excluded.video_name, ext=excluded.ext, title=excluded.title, year=excluded.year, size=excluded.size, mtime=excluded.mtime, missing=0, updated_at=excluded.updated_at", (path, folder, video_name, ext, title, year, size, mtime, added_at or now, now))
            self._conn.commit()
            row = self._conn.execute("SELECT id FROM movies WHERE path=?", (path,)).fetchone()
        return int(row["id"])

    def mark_missing_movies(self, seen_paths: set[str], before: str | None = None) -> int:
        """不在 seen_paths 里的在架记录标 missing。before：本轮扫描开始时刻——
        标记条件（missing=0 且 updated_at 早于 before）在每条 UPDATE 里原子判定，
        另一进程扫描期间 upsert 的路径 updated_at 已被刷新，不会被打回 missing
        （Kimi-D-3：原先先查后写，两步之间会把她进程刚确认的记录翻掉）。"""
        cutoff = before or utcnow()
        with self._lock:
            rows = self._conn.execute("SELECT path FROM movies WHERE missing=0").fetchall()
        gone = [r["path"] for r in rows if r["path"] not in seen_paths]
        marked = 0
        for p in gone:
            with self._lock:
                cur = self._conn.execute(
                    "UPDATE movies SET missing=1, updated_at=? WHERE path=? AND missing=0 AND (updated_at IS NULL OR updated_at < ?)",
                    (utcnow(), p, cutoff))
                self._conn.commit()
            marked += cur.rowcount
        return marked

    def people_hits(self, item_type: str, key: str) -> set[int]:
        """搜索词（已过 _search_key）命中演员/导演/主创名或角色名的 item_id 集合（10-07 用户要求）。
        name 是 TMDB zh-CN 译名，aka 是英文常用名＋TMDB 别名（原名、简繁中文译名等），角色名多为英文原文。"""
        if not key:
            return set()
        with self._lock:
            rows = self._conn.execute(
                "SELECT c.item_id, p.name, p.aka, p.aka_zh, c.character FROM credits c JOIN people p ON p.id=c.person_id "
                "WHERE c.item_type=?", (item_type,)).fetchall()
        return {r["item_id"] for r in rows
                if key in _search_key(r["name"]) or key in _search_key(r["aka"])
                or key in _search_key(r["aka_zh"])
                or key in _search_key(r["character"])}

    def get_movie(self, movie_id: int) -> sqlite3.Row | None:
        with self._lock:
            rows = self._conn.execute("SELECT m.*, p.position_sec, p.duration_sec, p.watched, p.updated_at AS played_at FROM movies m LEFT JOIN playback p ON p.item_type='movie' AND p.item_id=m.id WHERE m.id=?", (movie_id,)).fetchall()
        return rows[0] if rows else None

    def list_movies(self, q: str = "", sort: str = "title") -> list[sqlite3.Row]:
        with self._lock:
            rows = self._conn.execute("SELECT m.*, p.watched, p.position_sec, p.duration_sec, p.updated_at AS played_at FROM movies m LEFT JOIN playback p ON p.item_type='movie' AND p.item_id=m.id WHERE m.missing=0").fetchall()
        key = _search_key(q)
        hits = self.people_hits("movie", key)
        out = [r for r in rows if not key or _title_hit(r, key, hits)]
        if sort == "added":
            out.sort(key=lambda r: r["added_at"], reverse=True)
        elif sort == "year":
            out.sort(key=lambda r: (r["year"] is None, -(r["year"] or 0)))
        else:
            out.sort(key=lambda r: r["title"].lower())
        return out

    def movies_for_metadata(self, include_matched: bool = False) -> list[sqlite3.Row]:
        with self._lock:
            if include_matched:
                return self._conn.execute("SELECT * FROM movies WHERE missing=0 ORDER BY id").fetchall()
            return self._conn.execute("SELECT * FROM movies WHERE missing=0 AND status='unmatched' ORDER BY id").fetchall()

    def update_movie_metadata(self, movie_id: int, fields: dict) -> None:
        """固定列整句 UPDATE；未提供的字段保留原值。"""
        row = self.get_movie(movie_id)
        if row is None:
            return
        vals = [_meta_value(fields.get(c, row[c])) for c in _MOVIE_META_COLS]
        with self._lock:
            self._conn.execute("UPDATE movies SET tmdb_id=?, overview=?, genres=?, rating=?, runtime_minutes=?, poster_path=?, backdrop_path=?, poster_local=?, poster_cached=?, backdrop_cached=?, status=?, metadata_at=COALESCE(metadata_at, ?) WHERE id=?", (*vals, utcnow(), movie_id))
            self._conn.commit()

    # ---------- 同片多版本（tmdb_id 分组） ----------

    def list_movie_groups(self, q: str = "", sort: str = "added") -> list[dict]:
        """同 tmdb_id 的多个文件算一组，未匹配的各自一组。
        每组返回 {"row": 默认播放版本行, "count": 版本数, "version_ids": 组内全部在架文件 id,
        "added_at": 组内最新入库时间}。
        q 只决定哪些组入选：命中组里任意一个版本就整组入选，分组信息按整组算，
        不随搜索词变成命中版本的那个子集。"""
        groups: dict[tuple, list[sqlite3.Row]] = {}
        for r in self.list_movies():
            groups.setdefault(_movie_group_key(r), []).append(r)
        key = _search_key(q)
        hits = self.people_hits("movie", key)
        out = [{"row": max(ms, key=_default_version_key), "count": len(ms),
                "version_ids": [m["id"] for m in ms],
                "added_at": max(m["added_at"] for m in ms)}
               for ms in groups.values()
               if not key or any(_title_hit(m, key, hits) for m in ms)]
        if sort == "added":
            out.sort(key=lambda g: g["added_at"], reverse=True)
        elif sort == "year":
            out.sort(key=lambda g: (g["row"]["year"] is None, -(g["row"]["year"] or 0)))
        else:
            out.sort(key=lambda g: g["row"]["title"].lower())
        return out

    def movie_versions(self, movie_id: int) -> list[sqlite3.Row]:
        """这部片的全部在架文件：tmdb 匹配的共享同一条目，未匹配的只有自己。"""
        with self._lock:
            rows = self._conn.execute("SELECT m.*, p.position_sec, p.duration_sec, p.watched, p.updated_at AS played_at FROM movies m LEFT JOIN playback p ON p.item_type='movie' AND p.item_id=m.id WHERE m.missing=0 AND (m.id=? OR (m.tmdb_id IS NOT NULL AND m.tmdb_id=(SELECT tmdb_id FROM movies WHERE id=? AND tmdb_id IS NOT NULL)))", (movie_id, movie_id)).fetchall()
        if rows:
            return rows
        row = self.get_movie(movie_id)
        return [row] if row is not None else []

    # ---------- shows / seasons / episodes ----------

    def upsert_show(self, *, path: str, folder: str, title: str, year: int | None) -> int:
        now = utcnow()
        with self._lock:
            self._conn.execute("INSERT INTO shows (path, folder, title, year, added_at, updated_at) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(path) DO UPDATE SET folder=excluded.folder, title=excluded.title, year=excluded.year, updated_at=excluded.updated_at", (path, folder, title, year, now, now))
            self._conn.commit()
            row = self._conn.execute("SELECT id FROM shows WHERE path=?", (path,)).fetchone()
        return int(row["id"])

    def upsert_episode(self, *, show_id: int, season_number: int, episode_number: int,
                       path: str, video_name: str, ext: str, title: str,
                       size: int, mtime: float, added_at: str | None = None) -> int:
        now = utcnow()
        with self._lock:
            self._conn.execute("INSERT INTO episodes (show_id, season_number, episode_number, path, video_name, ext, title, size, mtime, added_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(path) DO UPDATE SET show_id=excluded.show_id, season_number=excluded.season_number, episode_number=excluded.episode_number, video_name=excluded.video_name, ext=excluded.ext, title=excluded.title, size=excluded.size, mtime=excluded.mtime, missing=0, updated_at=excluded.updated_at", (show_id, season_number, episode_number, path, video_name, ext, title, size, mtime, added_at or now, now))
            self._conn.commit()
            row = self._conn.execute("SELECT id FROM episodes WHERE path=?", (path,)).fetchone()
        return int(row["id"])

    def upsert_season(self, show_id: int, season_number: int, name: str | None = None,
                      episode_count: int | None = None, poster_path: str | None = None) -> None:
        with self._lock:
            self._conn.execute("INSERT INTO seasons (show_id, season_number, name, episode_count, poster_path) VALUES (?, ?, ?, ?, ?) ON CONFLICT(show_id, season_number) DO UPDATE SET name=COALESCE(excluded.name, seasons.name), episode_count=COALESCE(excluded.episode_count, seasons.episode_count), poster_path=COALESCE(excluded.poster_path, seasons.poster_path)", (show_id, season_number, name, episode_count, poster_path))
            self._conn.commit()

    def mark_missing_episodes(self, seen_paths: set[str], before: str | None = None) -> int:
        # Kimi-D-3：同 mark_missing_movies，标记条件写入时原子判定
        cutoff = before or utcnow()
        with self._lock:
            rows = self._conn.execute("SELECT path FROM episodes WHERE missing=0").fetchall()
        gone = [r["path"] for r in rows if r["path"] not in seen_paths]
        marked = 0
        for p in gone:
            with self._lock:
                cur = self._conn.execute(
                    "UPDATE episodes SET missing=1, updated_at=? WHERE path=? AND missing=0 AND (updated_at IS NULL OR updated_at < ?)",
                    (utcnow(), p, cutoff))
                self._conn.commit()
            marked += cur.rowcount
        return marked

    def refresh_show_missing(self) -> None:
        """show.missing = 没有任何在架剧集。"""
        with self._lock:
            self._conn.execute("UPDATE shows SET missing=CASE WHEN EXISTS(SELECT 1 FROM episodes e WHERE e.show_id=shows.id AND e.missing=0) THEN 0 ELSE 1 END")
            self._conn.commit()

    def refresh_show_added(self) -> None:
        """show.added_at = 最新一集的入库时间，剧集新增一集就排到「最近添加」前面。"""
        with self._lock:
            self._conn.execute("UPDATE shows SET added_at=COALESCE((SELECT MAX(e.added_at) FROM episodes e WHERE e.show_id=shows.id AND e.missing=0), added_at)")
            self._conn.commit()

    def get_show(self, show_id: int) -> sqlite3.Row | None:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM shows WHERE id=?", (show_id,)).fetchall()
        return rows[0] if rows else None

    def list_shows(self, q: str = "", sort: str = "title") -> list[sqlite3.Row]:
        with self._lock:
            rows = self._conn.execute("SELECT s.*, (SELECT COUNT(*) FROM episodes e WHERE e.show_id=s.id AND e.missing=0) AS episode_count, (SELECT COUNT(*) FROM episodes e WHERE e.show_id=s.id AND e.missing=0 AND EXISTS(SELECT 1 FROM playback p WHERE p.item_type='episode' AND p.item_id=e.id AND p.watched=1)) AS watched_count FROM shows s WHERE s.missing=0").fetchall()
        key = _search_key(q)
        hits = self.people_hits("show", key)
        out = [r for r in rows if not key or _title_hit(r, key, hits)]
        if sort == "added":
            out.sort(key=lambda r: r["added_at"], reverse=True)
        elif sort == "year":
            out.sort(key=lambda r: (r["year"] is None, -(r["year"] or 0)))
        else:
            out.sort(key=lambda r: r["title"].lower())
        return out

    def episodes_for_show(self, show_id: int) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute("SELECT e.*, p.position_sec, p.duration_sec, p.watched, p.updated_at AS played_at FROM episodes e LEFT JOIN playback p ON p.item_type='episode' AND p.item_id=e.id WHERE e.show_id=? ORDER BY e.season_number, e.episode_number", (show_id,)).fetchall()

    def get_episode(self, episode_id: int) -> sqlite3.Row | None:
        with self._lock:
            rows = self._conn.execute("SELECT e.*, s.title AS show_title, p.position_sec, p.duration_sec, p.watched, p.updated_at AS played_at FROM episodes e JOIN shows s ON s.id=e.show_id LEFT JOIN playback p ON p.item_type='episode' AND p.item_id=e.id WHERE e.id=?", (episode_id,)).fetchall()
        return rows[0] if rows else None

    def seasons_for_show(self, show_id: int) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute("SELECT * FROM seasons WHERE show_id=? ORDER BY season_number", (show_id,)).fetchall()

    def shows_for_metadata(self, include_matched: bool = False) -> list[sqlite3.Row]:
        with self._lock:
            if include_matched:
                return self._conn.execute("SELECT * FROM shows WHERE missing=0 ORDER BY id").fetchall()
            return self._conn.execute("SELECT * FROM shows WHERE missing=0 AND status='unmatched' ORDER BY id").fetchall()

    def set_aka(self, table: str, item_id: int, titles: list[str]) -> None:
        """存别名（中文译名、原名），换行分隔，只供搜索用。table: movies / shows。"""
        assert table in ("movies", "shows")
        with self._lock:
            self._conn.execute(f"UPDATE {table} SET aka=? WHERE id=?", ("\n".join(titles), item_id))
            self._conn.commit()

    def update_show_metadata(self, show_id: int, fields: dict) -> None:
        row = self.get_show(show_id)
        if row is None:
            return
        vals = [_meta_value(fields.get(c, row[c])) for c in _SHOW_META_COLS]
        with self._lock:
            self._conn.execute("UPDATE shows SET tmdb_id=?, overview=?, genres=?, rating=?, runtime_minutes=?, poster_path=?, backdrop_path=?, poster_local=?, poster_cached=?, backdrop_cached=?, status=?, metadata_at=COALESCE(metadata_at, ?) WHERE id=?", (*vals, utcnow(), show_id))
            self._conn.commit()

    def update_episode_metadata(self, episode_id: int, fields: dict) -> None:
        row = self.get_episode(episode_id)
        if row is None:
            return
        vals = [_meta_value(fields.get(c, row[c])) for c in _EPISODE_META_COLS]
        with self._lock:
            self._conn.execute("UPDATE episodes SET tmdb_id=?, overview=?, still_path=?, still_cached=?, runtime_minutes=?, air_date=?, title=?, metadata_at=COALESCE(metadata_at, ?) WHERE id=?", (*vals, utcnow(), episode_id))
            self._conn.commit()

    # ---------- playback ----------

    def get_playback(self, item_type: str, item_id: int) -> sqlite3.Row | None:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM playback WHERE item_type=? AND item_id=?", (item_type, item_id)).fetchall()
        return rows[0] if rows else None

    def save_playback(self, item_type: str, item_id: int, position_sec: float,
                      duration_sec: float | None, watched: bool) -> None:
        with self._lock:
            self._conn.execute("INSERT INTO playback (item_type, item_id, position_sec, duration_sec, watched, updated_at) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(item_type, item_id) DO UPDATE SET position_sec=excluded.position_sec, duration_sec=excluded.duration_sec, watched=excluded.watched, updated_at=excluded.updated_at", (item_type, item_id, float(position_sec), duration_sec, 1 if watched else 0, utcnow()))
            self._conn.commit()

    def set_watched(self, item_type: str, item_id: int, watched: bool) -> bool:
        """手动标记；没有 playback 记录也要建一条（duration 未知）。"""
        if item_type == "movie":
            with self._lock:
                exists = self._conn.execute("SELECT id FROM movies WHERE id=?", (item_id,)).fetchall()
        elif item_type == "episode":
            with self._lock:
                exists = self._conn.execute("SELECT id FROM episodes WHERE id=?", (item_id,)).fetchall()
        else:
            return False
        if not exists:
            return False
        existing = self.get_playback(item_type, item_id)
        position = existing["position_sec"] if existing else 0.0
        duration = existing["duration_sec"] if existing else None
        self.save_playback(item_type, item_id, position, duration, watched)
        return True

    # ---------- segments（片头/片尾识别结果） ----------

    def save_segments(self, episode_id: int, intro, credits, source: str) -> None:
        """intro / credits 都是 (start, end) 或 None；source: chapters/fingerprint/none。
        同一集重跑覆盖旧记录（PRIMARY KEY=episode_id）。"""
        intro_start, intro_end = intro if intro else (None, None)
        credits_start, credits_end = credits if credits else (None, None)
        with self._lock:
            self._conn.execute("INSERT INTO segments (episode_id, intro_start, intro_end, credits_start, credits_end, source, detected_at) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(episode_id) DO UPDATE SET intro_start=excluded.intro_start, intro_end=excluded.intro_end, credits_start=excluded.credits_start, credits_end=excluded.credits_end, source=excluded.source, detected_at=excluded.detected_at", (episode_id, intro_start, intro_end, credits_start, credits_end, source, utcnow()))
            self._conn.commit()

    def get_segments(self, episode_id: int) -> sqlite3.Row | None:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM segments WHERE episode_id=?", (episode_id,)).fetchall()
        return rows[0] if rows else None

    def seasons_needing_segments(self, show_id: int | None = None
                                 ) -> list[tuple[int, int, list[sqlite3.Row]]]:
        """返回 [(show_id, season_number, [该季全部在架集...])]。
        只要某季里有任何一集还没在 segments 表里，整季都返回（声纹比对需要整季）。"""
        with self._lock:
            if show_id is None:
                rows = self._conn.execute("SELECT * FROM episodes WHERE missing=0 ORDER BY show_id, season_number, episode_number").fetchall()
            else:
                rows = self._conn.execute("SELECT * FROM episodes WHERE missing=0 AND show_id=? ORDER BY season_number, episode_number", (show_id,)).fetchall()
            detected = {r["episode_id"] for r in self._conn.execute("SELECT episode_id FROM segments").fetchall()}
        groups: dict[tuple[int, int], list[sqlite3.Row]] = {}
        for r in rows:
            groups.setdefault((r["show_id"], r["season_number"]), []).append(r)
        out = []
        for (sid, sn), eps in groups.items():
            if any(e["id"] not in detected for e in eps):
                out.append((sid, sn, eps))
        return out

    # ---------- 社区片头片尾库 ----------

    def episodes_for_community(self, refresh: bool = False, only_status: str | None = None,
                               limit: int | None = None) -> list[sqlite3.Row]:
        """在架、所属剧已匹配 TMDB 的集；refresh=False 时跳过已查过的。
        only_status="none" 时只挑库里记为该状态的集（重查误记的 none），按 fetched_at 从旧到新。"""
        with self._lock:
            if only_status:
                sql = ("SELECT e.*, s.tmdb_id AS show_tmdb_id, s.imdb_id AS show_imdb_id, s.title AS show_title "
                       "FROM episodes e JOIN shows s ON s.id=e.show_id "
                       "JOIN community_segments c ON c.episode_id=e.id "
                       "WHERE e.missing=0 AND s.tmdb_id IS NOT NULL AND c.status=? "
                       "ORDER BY c.fetched_at, e.show_id, e.season_number, e.episode_number")
                args: list = [only_status]
                if limit is not None:
                    sql += " LIMIT ?"
                    args.append(int(limit))
                return self._conn.execute(sql, args).fetchall()
            if limit is not None:
                raise ValueError("limit 只在 only_status 下生效")
            if refresh:
                return self._conn.execute("SELECT e.*, s.tmdb_id AS show_tmdb_id, s.imdb_id AS show_imdb_id, s.title AS show_title FROM episodes e JOIN shows s ON s.id=e.show_id WHERE e.missing=0 AND s.tmdb_id IS NOT NULL ORDER BY e.show_id, e.season_number, e.episode_number").fetchall()
            return self._conn.execute("SELECT e.*, s.tmdb_id AS show_tmdb_id, s.imdb_id AS show_imdb_id, s.title AS show_title FROM episodes e JOIN shows s ON s.id=e.show_id WHERE e.missing=0 AND s.tmdb_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM community_segments c WHERE c.episode_id=e.id) ORDER BY e.show_id, e.season_number, e.episode_number").fetchall()

    def save_community_segment(self, episode_id: int, provider: str | None, status: str,
                               seg: dict) -> None:
        """seg 键：intro_start/intro_end/recap_start/recap_end/credits_start/credits_end/preview_start（秒，可缺）。"""
        g = seg.get
        with self._lock:
            self._conn.execute("INSERT INTO community_segments (episode_id, provider, status, intro_start, intro_end, recap_start, recap_end, credits_start, credits_end, preview_start, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(episode_id) DO UPDATE SET provider=excluded.provider, status=excluded.status, intro_start=excluded.intro_start, intro_end=excluded.intro_end, recap_start=excluded.recap_start, recap_end=excluded.recap_end, credits_start=excluded.credits_start, credits_end=excluded.credits_end, preview_start=excluded.preview_start, fetched_at=excluded.fetched_at", (episode_id, provider, status, g("intro_start"), g("intro_end"), g("recap_start"), g("recap_end"), g("credits_start"), g("credits_end"), g("preview_start"), utcnow()))
            self._conn.commit()

    def get_community_segment(self, episode_id: int) -> sqlite3.Row | None:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM community_segments WHERE episode_id=?", (episode_id,)).fetchall()
        return rows[0] if rows else None

    def set_show_imdb(self, show_id: int, imdb_id: str | None) -> None:
        with self._lock:
            self._conn.execute("UPDATE shows SET imdb_id=? WHERE id=?", (imdb_id, show_id))
            self._conn.commit()

    # ---------- 演职员 / 合集（extras.py 写入） ----------

    def items_needing_extras(self, refresh: bool = False) -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
        with self._lock:
            if refresh:
                movies = self._conn.execute("SELECT * FROM movies WHERE missing=0 AND tmdb_id IS NOT NULL ORDER BY id").fetchall()
                shows = self._conn.execute("SELECT * FROM shows WHERE missing=0 AND tmdb_id IS NOT NULL ORDER BY id").fetchall()
            else:
                movies = self._conn.execute("SELECT * FROM movies WHERE missing=0 AND tmdb_id IS NOT NULL AND credits_at IS NULL ORDER BY id").fetchall()
                shows = self._conn.execute("SELECT * FROM shows WHERE missing=0 AND tmdb_id IS NOT NULL AND credits_at IS NULL ORDER BY id").fetchall()
        return movies, shows

    def people_needing_aka(self, refresh: bool = False) -> list[int]:
        """出现在演职员表里、还没补过别名的人（refresh=True 全部）。"""
        sql = "SELECT id FROM people WHERE id IN (SELECT person_id FROM credits)"
        if not refresh:
            sql += " AND aka_at IS NULL"
        with self._lock:
            return [r["id"] for r in self._conn.execute(sql + " ORDER BY id").fetchall()]

    def set_person_aka(self, person_id: int, names: list[str]) -> None:
        with self._lock:
            self._conn.execute("UPDATE people SET aka=?, aka_at=? WHERE id=?",
                               ("\n".join(names), utcnow(), person_id))
            self._conn.commit()

    def set_person_aka_zh(self, person_id: int, names: list[str]) -> bool:
        """写 Wikidata 中文名；人不在库里返回 False。"""
        with self._lock:
            cur = self._conn.execute("UPDATE people SET aka_zh=? WHERE id=?", ("\n".join(names), person_id))
            self._conn.commit()
        return cur.rowcount > 0

    def save_credits(self, item_type: str, item_id: int, people: list[dict]) -> None:
        """整批替换这部片的演职员；people 每项 {id, name, profile_path, profile_cached, role, character, ord}。"""
        with self._lock:
            # audit N01：整批替换是原子的。DELETE 与后续 INSERT 共用一个事务，
            # 中途异常必须回滚，否则悬挂的 DELETE 会被之后任意一次 commit 冲掉（旧演职员静默丢失）。
            self._conn.execute("SAVEPOINT save_credits")
            try:
                self._conn.execute("DELETE FROM credits WHERE item_type=? AND item_id=?", (item_type, item_id))
                for p in people:
                    self._conn.execute("INSERT INTO people (id, name, profile_path, profile_cached) VALUES (?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET name=excluded.name, profile_path=COALESCE(excluded.profile_path, people.profile_path), profile_cached=COALESCE(excluded.profile_cached, people.profile_cached)", (p["id"], p["name"], p.get("profile_path"), p.get("profile_cached")))
                    self._conn.execute("INSERT OR IGNORE INTO credits (item_type, item_id, person_id, role, character, ord) VALUES (?, ?, ?, ?, ?, ?)", (item_type, item_id, p["id"], p["role"], p.get("character"), p.get("ord", 0)))
            except Exception:
                self._conn.execute("ROLLBACK TO save_credits")
                self._conn.execute("RELEASE save_credits")
                raise
            self._conn.execute("RELEASE save_credits")
            self._conn.commit()

    def upsert_collection(self, coll_id: int, name: str, poster_cached: str | None,
                          parts: list[dict] | None = None) -> None:
        """parts：合集全部影片 [{tmdb_id, title, year}]；拉取失败传 None 保留上次的。"""
        parts_json = json.dumps(parts, ensure_ascii=False) if parts is not None else None
        with self._lock:
            self._conn.execute("INSERT INTO collections (id, name, poster_cached, parts) VALUES (?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET name=excluded.name, poster_cached=COALESCE(excluded.poster_cached, collections.poster_cached), parts=COALESCE(excluded.parts, collections.parts)", (coll_id, name, poster_cached, parts_json))
            self._conn.commit()

    def collection_has_parts(self, coll_id: int) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT parts FROM collections WHERE id=?", (coll_id,)).fetchone()
        return bool(row and row["parts"])

    def set_movie_extras(self, movie_id: int, collection_id: int | None) -> None:
        with self._lock:
            self._conn.execute("UPDATE movies SET collection_id=?, credits_at=? WHERE id=?", (collection_id, utcnow(), movie_id))
            self._conn.commit()

    def set_show_airing(self, show_id: int, status: str | None, next_air: str | None,
                        last_air: str | None, credits_done: bool = False) -> None:
        now = utcnow()
        with self._lock:
            if credits_done:
                self._conn.execute("UPDATE shows SET tmdb_status=?, next_air=?, last_air=?, airing_at=?, credits_at=? WHERE id=?", (status, next_air, last_air, now, now, show_id))
            else:
                self._conn.execute("UPDATE shows SET tmdb_status=?, next_air=?, last_air=?, airing_at=? WHERE id=?", (status, next_air, last_air, now, show_id))
            self._conn.commit()

    def credits_for(self, item_type: str, item_id: int) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute("SELECT c.role, c.character, c.ord, p.id, p.name, p.profile_cached FROM credits c JOIN people p ON p.id=c.person_id WHERE c.item_type=? AND c.item_id=? ORDER BY c.role, c.ord", (item_type, item_id)).fetchall()

    def get_person(self, person_id: int) -> sqlite3.Row | None:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM people WHERE id=?", (person_id,)).fetchall()
        return rows[0] if rows else None

    def works_of_person(self, person_id: int) -> tuple[list[tuple[str, sqlite3.Row]], list[tuple[str, sqlite3.Row]]]:
        """(作为导演/主创的作品, 作为演员的作品)，每项 (role, 片子行)；只含在架的。"""
        with self._lock:
            refs = self._conn.execute("SELECT item_type, item_id, role, character FROM credits WHERE person_id=?", (person_id,)).fetchall()
        made, acted = [], []
        for r in refs:
            if r["item_type"] == "movie":
                row = self.get_movie(r["item_id"])
            else:
                rows = self.list_shows_by_ids([r["item_id"]])
                row = rows[0] if rows else None
            if row is None or row["missing"]:
                continue
            (acted if r["role"] == "cast" else made).append((r["item_type"], row, r["character"]))
        return made, acted

    def list_shows_by_ids(self, ids: list[int]) -> list[sqlite3.Row]:
        wanted = set(ids)
        return [r for r in self.list_shows() if r["id"] in wanted]

    def people_by_role(self, roles: tuple[str, ...] = ("director", "creator"),
                       collapse_movies: bool = False) -> list[sqlite3.Row]:
        """导演/主创及其在架作品数，按作品数降序。
        collapse_movies：同 tmdb_id 的多个版本算一部（口味画像的「部数」口径）。"""
        with self._lock:
            rows = self._conn.execute("SELECT p.id, p.name, p.profile_cached, c.role, c.item_type, c.item_id, m.tmdb_id AS movie_tmdb_id FROM credits c JOIN people p ON p.id=c.person_id LEFT JOIN movies m ON c.item_type='movie' AND m.id=c.item_id LEFT JOIN shows s ON c.item_type='show' AND s.id=c.item_id WHERE COALESCE(m.missing, s.missing, 1)=0").fetchall()
        agg: dict[int, dict] = {}
        for r in rows:
            if r["role"] not in roles:
                continue
            a = agg.setdefault(r["id"], {"id": r["id"], "name": r["name"], "profile": r["profile_cached"], "items": set()})
            if collapse_movies and r["item_type"] == "movie":
                a["items"].add(("tmdb", r["movie_tmdb_id"]) if r["movie_tmdb_id"] is not None
                               else ("id", r["item_id"]))
            else:
                a["items"].add((r["item_type"], r["item_id"]))
        out = [{**a, "count": len(a.pop("items"))} for a in agg.values()]
        out.sort(key=lambda a: (-a["count"], a["name"].lower()))
        return out

    def list_collections(self) -> list[dict]:
        """库里至少有一部的合集，附在架部数。"""
        with self._lock:
            rows = self._conn.execute("SELECT c.id, c.name, c.poster_cached, COUNT(m.id) AS owned FROM collections c JOIN movies m ON m.collection_id=c.id AND m.missing=0 GROUP BY c.id ORDER BY owned DESC, c.name").fetchall()
        return [dict(r) for r in rows]

    def get_collection(self, coll_id: int) -> sqlite3.Row | None:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM collections WHERE id=?", (coll_id,)).fetchall()
        return rows[0] if rows else None

    def movies_in_collection(self, coll_id: int) -> list[sqlite3.Row]:
        return sorted((r for r in self.list_movies() if r["collection_id"] == coll_id),
                      key=lambda r: (r["year"] is None, r["year"] or 0))

    # ---------- 收藏 ----------

    def set_favorite(self, item_type: str, item_id: int, favorite: bool) -> bool:
        if item_type == "movie":
            ok = self.get_movie(item_id) is not None
        elif item_type == "show":
            ok = self.get_show(item_id) is not None
        else:
            return False
        if not ok:
            return False
        with self._lock:
            if favorite:
                self._conn.execute("INSERT INTO favorites (item_type, item_id, added_at) VALUES (?, ?, ?) ON CONFLICT(item_type, item_id) DO NOTHING", (item_type, item_id, utcnow()))
            else:
                self._conn.execute("DELETE FROM favorites WHERE item_type=? AND item_id=?", (item_type, item_id))
            self._conn.commit()
        return True

    def favorite_ids(self, item_type: str) -> set[int]:
        with self._lock:
            rows = self._conn.execute("SELECT item_id FROM favorites WHERE item_type=?", (item_type,)).fetchall()
        return {int(r["item_id"]) for r in rows}

    # ---------- 追剧日历 ----------

    def shows_for_airing(self) -> list[sqlite3.Row]:
        """没完结或还没拉过状态的已匹配剧。"""
        with self._lock:
            return self._conn.execute("SELECT * FROM shows WHERE missing=0 AND tmdb_id IS NOT NULL AND (tmdb_status IS NULL OR tmdb_status NOT IN ('Ended', 'Canceled')) ORDER BY id").fetchall()

    def airing_refreshed_at(self) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT MAX(airing_at) AS ts FROM shows WHERE missing=0").fetchone()
        return row["ts"] if row else None

    def latest_local_episode(self, show_id: int) -> tuple[int, int] | None:
        with self._lock:
            row = self._conn.execute("SELECT season_number, episode_number FROM episodes WHERE show_id=? AND missing=0 AND season_number>0 ORDER BY season_number DESC, episode_number DESC LIMIT 1", (show_id,)).fetchone()
        return (row["season_number"], row["episode_number"]) if row else None

    def missing_episodes(self, show_id: int) -> list[dict]:
        """季内缺集：库里有这一季、TMDB 给了集数，按已播出的集数比对。
        返回 [{"season": n, "episodes": [缺的集号...]}]；正片季（季号>0）才算。"""
        show = self.get_show(show_id)
        if show is None:
            return []
        aired_cap: dict[int, int] = {}
        for key in ("next_air", "last_air"):
            try:
                info = json.loads(show[key]) if show[key] else None
            except ValueError:
                info = None
            if info and info.get("season") and info.get("episode"):
                cap = info["episode"] - (1 if key == "next_air" else 0)
                aired_cap[info["season"]] = cap if key == "next_air" else max(aired_cap.get(info["season"], 0), cap)
        with self._lock:
            seasons = self._conn.execute("SELECT season_number, episode_count FROM seasons WHERE show_id=? AND season_number>0 AND episode_count>0", (show_id,)).fetchall()
            have_rows = self._conn.execute("SELECT season_number, episode_number, video_name FROM episodes WHERE show_id=? AND missing=0", (show_id,)).fetchall()
        have: dict[int, set[int]] = {}
        for r in have_rows:
            have.setdefault(r["season_number"], set()).update(_episode_span(r["episode_number"], r["video_name"]))
        out = []
        for s in seasons:
            sn = s["season_number"]
            if sn not in have:
                continue  # 整季没有：多半是没下，不算缺集（已播出未入库的在日历里另列）
            count = min(s["episode_count"], aired_cap.get(sn, s["episode_count"]))
            if sn == max(have):
                # 库里最后一季：结尾没下的集算「已播出未入库」（日历另列），这里只算中间的空洞
                count = min(count, max(have[sn]))
            gap = [n for n in range(1, count + 1) if n not in have[sn]]
            if gap:
                out.append({"season": sn, "episodes": gap})
        return out

    def set_video_height(self, movie_id: int, height: int | None) -> None:
        with self._lock:
            self._conn.execute("UPDATE movies SET video_height=? WHERE id=?", (height, movie_id))
            self._conn.commit()

    # ---------- 下载的字幕 ----------

    def add_subtitle(self, item_type: str, item_id: int, path: str, sub_id: int | None) -> None:
        with self._lock:
            self._conn.execute("INSERT INTO subtitles (item_type, item_id, path, sub_id, added_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT(item_type, item_id, path) DO UPDATE SET added_at=excluded.added_at", (item_type, item_id, path, sub_id, utcnow()))
            self._conn.commit()

    def subtitles_for(self, item_type: str, item_id: int) -> list[str]:
        """已下载、文件还在的字幕路径（新下的在前）。"""
        with self._lock:
            rows = self._conn.execute("SELECT path FROM subtitles WHERE item_type=? AND item_id=? ORDER BY added_at DESC", (item_type, item_id)).fetchall()
        return [r["path"] for r in rows if Path(r["path"]).is_file()]

    def remove_subtitle(self, item_type: str, item_id: int, path: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM subtitles WHERE item_type=? AND item_id=? AND path=?", (item_type, item_id, path))
            self._conn.commit()
        return cur.rowcount > 0

    # ---------- 首页聚合 ----------

    def next_episode(self, show_id: int, season_number: int, episode_number: int
                     ) -> sqlite3.Row | None:
        """同季的下一集；季末则下一季第一集。只看在架（missing=0）的集。"""
        with self._lock:
            rows = self._conn.execute("SELECT * FROM episodes WHERE show_id=? AND missing=0 AND ((season_number=? AND episode_number>?) OR season_number>?) ORDER BY season_number, episode_number LIMIT 1", (show_id, season_number, episode_number, season_number)).fetchall()
        return rows[0] if rows else None

    def first_episode(self, show_id: int) -> sqlite3.Row | None:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM episodes WHERE show_id=? AND missing=0 ORDER BY season_number, episode_number LIMIT 1", (show_id,)).fetchall()
        return rows[0] if rows else None

    def next_up_for_show(self, show_id: int) -> tuple[sqlite3.Row, str] | None:
        """「在看剧集的下一集」：返回 (episode 行, 来源)。
        有进行中的集（位置>=30 秒且未看完）→ 继续该集；
        否则最后看完那集的下一集（季末跨季）；全看完 → None；从未看过 → (第一集, 'first')。"""
        with self._lock:
            rows = self._conn.execute("SELECT e.*, p.updated_at AS played_at FROM episodes e JOIN playback p ON p.item_type='episode' AND p.item_id=e.id WHERE e.show_id=? AND e.missing=0 AND p.watched=0 AND p.position_sec>=30 ORDER BY p.updated_at DESC LIMIT 1", (show_id,)).fetchall()
        if rows:
            return rows[0], "resume"
        with self._lock:
            rows = self._conn.execute("SELECT e.*, p.updated_at AS played_at FROM episodes e JOIN playback p ON p.item_type='episode' AND p.item_id=e.id WHERE e.show_id=? AND p.watched=1 ORDER BY p.updated_at DESC LIMIT 1", (show_id,)).fetchall()
        if rows:
            last = rows[0]
            nxt = self.next_episode(show_id, last["season_number"],
                                    last["episode_number"])
            if nxt is None:
                return None
            return nxt, "next"
        first = self.first_episode(show_id)
        if first is None:
            return None
        return first, "first"

    def continue_watching(self, limit: int = 20) -> list[dict]:
        items: list[dict] = []
        with self._lock:
            movie_rows = self._conn.execute("SELECT m.*, p.position_sec, p.duration_sec, p.updated_at AS played_at FROM movies m JOIN playback p ON p.item_type='movie' AND p.item_id=m.id WHERE m.missing=0 AND p.watched=0 AND p.position_sec>=30").fetchall()
            show_rows = self._conn.execute("SELECT DISTINCT s.* FROM shows s JOIN episodes e ON e.show_id=s.id JOIN playback p ON p.item_type='episode' AND p.item_id=e.id WHERE s.missing=0 ORDER BY s.id").fetchall()
        # 同一 tmdb_id 的多个版本只留最近播放的那个，一张卡
        best: dict[tuple, sqlite3.Row] = {}
        for r in movie_rows:
            key = _movie_group_key(r)
            cur = best.get(key)
            if cur is None or (r["played_at"] or "") > (cur["played_at"] or ""):
                best[key] = r
        for r in best.values():
            items.append({
                "type": "movie", "id": r["id"], "title": r["title"], "year": r["year"],
                "poster": r["poster_cached"], "backdrop": r["backdrop_cached"],
                "position_sec": r["position_sec"], "duration_sec": r["duration_sec"],
                "progress": _progress(r["position_sec"], r["duration_sec"]),
                "updated_at": r["played_at"], "href": f"#/movie/{r['id']}",
            })
        for show in show_rows:
            pair = self.next_up_for_show(show["id"])
            if pair is None:
                continue
            ep, _source = pair
            pb = self.get_playback("episode", ep["id"])
            # 排序按这部剧最近一次播放活动：「该看下一集」的卡片下一集还没有 playback，
            # 不能拿它的 updated_at（None 会让追剧卡永远沉底）
            with self._lock:
                last = self._conn.execute("SELECT MAX(p.updated_at) FROM playback p JOIN episodes e ON p.item_type='episode' AND p.item_id=e.id WHERE e.show_id=?", (show["id"],)).fetchone()[0]
            position = pb["position_sec"] if pb else 0.0
            duration = pb["duration_sec"] if pb else None
            watched = bool(pb and pb["watched"])
            items.append({
                "type": "episode", "id": ep["id"], "show_id": show["id"],
                "show_title": show["title"],
                "season_number": ep["season_number"], "episode_number": ep["episode_number"],
                "title": ep["title"],
                "poster": show["poster_cached"], "backdrop": show["backdrop_cached"],
                "still": ep["still_cached"],
                "position_sec": position, "duration_sec": duration,
                "watched": watched,
                "progress": 1.0 if watched else _progress(position, duration),
                "updated_at": last,
                "href": f"#/show/{show['id']}",
            })
        items.sort(key=lambda x: x["updated_at"] or "", reverse=True)
        return items[:limit]

    def recently_added(self, limit: int = 20) -> list[dict]:
        """电影一部一张卡（同 tmdb_id 多版本合一张，按组内最新入库时间排），
        剧集一部一张卡（按最新一集的入库时间），不按单集刷屏。"""
        groups = self.list_movie_groups(sort="added")
        with self._lock:
            show_rows = self._conn.execute("SELECT id, title, year, poster_cached, added_at FROM shows WHERE missing=0").fetchall()
        entries = [{
            "type": "movie", "id": g["row"]["id"], "title": g["row"]["title"],
            "year": g["row"]["year"], "poster": g["row"]["poster_cached"],
            "version_count": g["count"], "added_at": g["added_at"],
            "href": f"#/movie/{g['row']['id']}",
        } for g in groups]
        entries += [{
            "type": "show", "id": r["id"], "title": r["title"], "year": r["year"],
            "poster": r["poster_cached"], "added_at": r["added_at"],
            "href": f"#/show/{r['id']}",
        } for r in show_rows]
        entries.sort(key=lambda x: x["added_at"], reverse=True)
        return entries[:limit]

    # ---------- 推荐 ----------

    def all_tmdb_ids(self) -> dict[str, set[int]]:
        """库里已有的 TMDB 编号（含 missing=1 的条目，它们仍算「已拥有」）。"""
        with self._lock:
            movies = {int(r["tmdb_id"]) for r in self._conn.execute("SELECT tmdb_id FROM movies WHERE tmdb_id IS NOT NULL").fetchall()}
            shows = {int(r["tmdb_id"]) for r in self._conn.execute("SELECT tmdb_id FROM shows WHERE tmdb_id IS NOT NULL").fetchall()}
        return {"movie": movies, "tv": shows}

    def dismissed_recommendations(self) -> set[tuple[str, int]]:
        with self._lock:
            rows = self._conn.execute("SELECT kind, tmdb_id FROM rec_dismissed").fetchall()
        return {(r["kind"], int(r["tmdb_id"])) for r in rows}

    def save_recommendations(self, rows: list[dict]) -> None:
        """整批替换：先清空再写入，同一事务内完成。"""
        cols = ("kind", "tmdb_id", "title", "original_title", "year", "overview",
                "poster_cached", "vote_average", "vote_count", "score", "because",
                "fit", "generated_at")
        with self._lock:
            self._conn.execute("DELETE FROM recommendations")
            self._conn.executemany(
                "INSERT INTO recommendations (kind, tmdb_id, title, original_title, year, overview, poster_cached, vote_average, vote_count, score, because, fit, generated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [tuple(r.get(c) for c in cols) for r in rows])
            self._conn.commit()

    def list_recommendations(self) -> list[sqlite3.Row]:
        """按 score 降序；kind 分组由调用方处理。"""
        with self._lock:
            return self._conn.execute("SELECT * FROM recommendations ORDER BY score DESC").fetchall()

    def dismiss_recommendation(self, kind: str, tmdb_id: int) -> None:
        """记进「不感兴趣」并从推荐表删掉这一条。"""
        with self._lock:
            self._conn.execute("INSERT INTO rec_dismissed (kind, tmdb_id, dismissed_at) VALUES (?, ?, ?) ON CONFLICT(kind, tmdb_id) DO UPDATE SET dismissed_at=excluded.dismissed_at", (kind, int(tmdb_id), utcnow()))
            self._conn.execute("DELETE FROM recommendations WHERE kind=? AND tmdb_id=?", (kind, int(tmdb_id)))
            self._conn.commit()

    def recommendations_generated_at(self) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT MAX(generated_at) AS ts FROM recommendations").fetchone()
        return row["ts"] if row else None


_MULTI_EP_RE = re.compile(r"S\d{1,2}((?:[-_ ]?E\d{1,4}){2,})", re.IGNORECASE)

_YEAR_PAREN_RE = re.compile(r"[(（]\d{4}[)）]")


def _movie_group_key(row) -> tuple:
    return ("tmdb", row["tmdb_id"]) if row["tmdb_id"] is not None else ("id", row["id"])


def _default_version_key(row) -> tuple:
    """默认播放版本的排序键：播过且播的时间最新优先；都没播过取分辨率高、文件大的。
    只按规格取高，不猜音轨语言。"""
    played = row["played_at"] if "played_at" in row.keys() else None
    height = row["video_height"] if "video_height" in row.keys() else None
    return (1 if played else 0, played or "", int(height or 0), int(row["size"] or 0))


def _version_label(folder: str, video_name: str, title: str | None,
                   ext: str, height: int | None) -> str:
    """版本名：文件夹名/文件名里片名年份之后的后缀（如「Ultimate Cut」）；
    没有后缀用 `分辨率 · 扩展名`。"""
    def tail(name: str) -> str:
        m = _YEAR_PAREN_RE.search(name)
        if m:
            rest = name[m.end():]
        elif title and name.lower().startswith(title.lower()):
            rest = name[len(title):]
        else:
            rest = ""
        return rest.strip(" -_·.—")

    stem = video_name.rsplit(".", 1)[0] if "." in video_name else video_name
    for name in (folder.rsplit("/", 1)[-1], stem):
        label = tail(name)
        if label:
            return label
    return f"{height}p · {ext}" if height else ext


def _episode_span(episode_number: int, video_name: str) -> range:
    """多集合并的文件（S01E01E02、S01E001-E004）覆盖的集号；扫描只记了第一集。"""
    m = _MULTI_EP_RE.search(video_name or "")
    if m:
        nums = [int(n) for n in re.findall(r"\d+", m.group(1))]
        if nums[0] == episode_number and nums[-1] > nums[0] and nums[-1] - nums[0] < 20:
            return range(nums[0], nums[-1] + 1)
    return range(episode_number, episode_number + 1)


def _meta_value(value):
    """genres 统一存 JSON 字符串，其余原样。"""
    if isinstance(value, (list, tuple)):
        return json.dumps(value, ensure_ascii=False)
    return value


def _progress(position: float | None, duration: float | None) -> float | None:
    if position is None or not duration:
        return None
    return max(0.0, min(1.0, position / duration))
