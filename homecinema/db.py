"""SQLite 存储层。所有读写经单个连接 + RLock，供后台扫描线程与请求线程共用。

约定：SQL 一律是 execute() 调用处的内联单行字面量 + 参数占位符，不做任何拼接。
"""

from __future__ import annotations

import json
import sqlite3
import threading
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
CREATE INDEX IF NOT EXISTS idx_episodes_show ON episodes(show_id, season_number, episode_number);
CREATE INDEX IF NOT EXISTS idx_playback_updated ON playback(updated_at);
"""

_MOVIE_META_COLS = ("tmdb_id", "overview", "genres", "rating", "runtime_minutes",
                    "poster_path", "backdrop_path", "poster_local", "poster_cached",
                    "backdrop_cached", "status")
_SHOW_META_COLS = _MOVIE_META_COLS
_EPISODE_META_COLS = ("tmdb_id", "overview", "still_path", "still_cached",
                      "runtime_minutes", "air_date", "title")


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

    def mark_missing_movies(self, seen_paths: set[str]) -> int:
        with self._lock:
            rows = self._conn.execute("SELECT path FROM movies WHERE missing=0").fetchall()
        gone = [r["path"] for r in rows if r["path"] not in seen_paths]
        for p in gone:
            with self._lock:
                self._conn.execute("UPDATE movies SET missing=1, updated_at=? WHERE path=?", (utcnow(), p))
                self._conn.commit()
        return len(gone)

    def get_movie(self, movie_id: int) -> sqlite3.Row | None:
        with self._lock:
            rows = self._conn.execute("SELECT m.*, p.position_sec, p.duration_sec, p.watched, p.updated_at AS played_at FROM movies m LEFT JOIN playback p ON p.item_type='movie' AND p.item_id=m.id WHERE m.id=?", (movie_id,)).fetchall()
        return rows[0] if rows else None

    def list_movies(self, q: str = "", sort: str = "title") -> list[sqlite3.Row]:
        with self._lock:
            rows = self._conn.execute("SELECT m.*, p.watched, p.position_sec, p.duration_sec FROM movies m LEFT JOIN playback p ON p.item_type='movie' AND p.item_id=m.id WHERE m.missing=0").fetchall()
        out = [r for r in rows if not q or q.lower() in r["title"].lower()]
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

    def mark_missing_episodes(self, seen_paths: set[str]) -> int:
        with self._lock:
            rows = self._conn.execute("SELECT path FROM episodes WHERE missing=0").fetchall()
        gone = [r["path"] for r in rows if r["path"] not in seen_paths]
        for p in gone:
            with self._lock:
                self._conn.execute("UPDATE episodes SET missing=1, updated_at=? WHERE path=?", (utcnow(), p))
                self._conn.commit()
        return len(gone)

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
        out = [r for r in rows if not q or q.lower() in r["title"].lower()]
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
        for r in movie_rows:
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
                "updated_at": pb["updated_at"] if pb else None,
                "href": f"#/show/{show['id']}",
            })
        items.sort(key=lambda x: x["updated_at"] or "", reverse=True)
        return items[:limit]

    def recently_added(self, limit: int = 20) -> list[dict]:
        """电影一部一张卡，剧集一部一张卡（按最新一集的入库时间），不按单集刷屏。"""
        with self._lock:
            rows = self._conn.execute("SELECT id, title, year, poster_cached, added_at, 'movie' AS kind FROM movies WHERE missing=0 UNION ALL SELECT id, title, year, poster_cached, added_at, 'show' AS kind FROM shows WHERE missing=0 ORDER BY added_at DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            href = f"#/movie/{r['id']}" if r["kind"] == "movie" else f"#/show/{r['id']}"
            out.append({
                "type": r["kind"], "id": r["id"], "title": r["title"], "year": r["year"],
                "poster": r["poster_cached"], "added_at": r["added_at"], "href": href,
            })
        return out

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
                "poster_cached", "vote_average", "vote_count", "score", "because", "generated_at")
        with self._lock:
            self._conn.execute("DELETE FROM recommendations")
            self._conn.executemany(
                "INSERT INTO recommendations (kind, tmdb_id, title, original_title, year, overview, poster_cached, vote_average, vote_count, score, because, generated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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


def _meta_value(value):
    """genres 统一存 JSON 字符串，其余原样。"""
    if isinstance(value, (list, tuple)):
        return json.dumps(value, ensure_ascii=False)
    return value


def _progress(position: float | None, duration: float | None) -> float | None:
    if position is None or not duration:
        return None
    return max(0.0, min(1.0, position / duration))
