"""FastAPI 服务：浏览、搜索、播放、进度、扫描。只监听 127.0.0.1（见 __main__）。"""

from __future__ import annotations

import json
import mimetypes
import os
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import Config
from .db import Library, utcnow
from .metadata import MetadataService, TmdbClient
from .player import Player, PlayerError
from .recommend import generate
from .scanner import run_scan

WEB_DIR = Path(__file__).resolve().parent / "web"
# App 外壳据此判断现有服务是不是在代码更新之前起的（是就重启它，免得沿用旧代码）
SERVER_STARTED = time.time()


class PlayBody(BaseModel):
    type: str
    id: int
    embedded: bool = False


class ProgressBody(BaseModel):
    type: str
    id: int
    position: float
    duration: float | None = None


class WatchedBody(BaseModel):
    type: str
    id: int
    watched: bool


class ScanService:
    """后台扫描 + 补元数据；/api/scan/status 读这里的进度。"""

    def __init__(self, config: Config, db: Library):
        self.config = config
        self.db = db
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._phase = "idle"
        self._counts: dict = {}
        self._meta_done = 0
        self._meta_total = 0
        self._started_at: str | None = None
        self._finished_at: str | None = None
        self._error: str | None = None

    def start(self) -> bool:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._phase = "scanning"
            self._counts = {}
            self._meta_done = 0
            self._meta_total = 0
            self._error = None
            self._started_at = utcnow()
            self._finished_at = None
            self._thread = threading.Thread(target=self._run, daemon=True, name="scan")
            self._thread.start()
            return True

    def _run(self) -> None:
        try:
            def scan_progress(phase: str, done: int, total: int) -> None:
                with self._lock:
                    self._phase = phase

            self._counts = run_scan(self.config, self.db, progress=scan_progress)
            key = self.config.read_tmdb_key()
            if key:
                with self._lock:
                    self._phase = "metadata"
                client = TmdbClient(key, self.config.tmdb_language)
                service = MetadataService(self.db, client, self.config.images_dir,
                                          progress=self._meta_progress)
                service.enrich_all()
        except Exception as exc:  # 后台线程兜底，错误在 status 里可见
            with self._lock:
                self._error = f"{type(exc).__name__}: {exc}"
        finally:
            with self._lock:
                self._phase = "idle"
                self._finished_at = utcnow()

    def _meta_progress(self, done: int, total: int, label: str) -> None:
        with self._lock:
            self._meta_done = done
            self._meta_total = total

    def status(self) -> dict:
        with self._lock:
            running = self._thread is not None and self._thread.is_alive()
            return {
                "running": running,
                "phase": self._phase,
                "movies": self._counts.get("movies", 0),
                "episodes": self._counts.get("episodes", 0),
                "missing_movies": self._counts.get("missing_movies", 0),
                "missing_episodes": self._counts.get("missing_episodes", 0),
                "metadata_done": self._meta_done,
                "metadata_total": self._meta_total,
                "started_at": self._started_at,
                "finished_at": self._finished_at,
                "error": self._error,
            }


class RecService:
    """后台生成推荐；/api/recommendations/status 读这里的进度。

    client_factory 可注入：测试传假 TMDB 客户端，默认按 config 的 key 建 TmdbClient。"""

    def __init__(self, config: Config, db: Library, client_factory=None):
        self.config = config
        self.db = db
        self._client_factory = client_factory
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._phase = "idle"
        self._done = 0
        self._total = 0
        self._counts: dict = {}
        self._started_at: str | None = None
        self._finished_at: str | None = None
        self._error: str | None = None

    def _make_client(self):
        if self._client_factory is not None:
            return self._client_factory()
        key = self.config.read_tmdb_key()
        if not key:
            raise RuntimeError("没有配置 TMDB API key")
        return TmdbClient(key, self.config.tmdb_language)

    def start(self, refresh: bool = True) -> bool:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._phase = "running"
            self._done = 0
            self._total = 0
            self._counts = {}
            self._error = None
            self._started_at = utcnow()
            self._finished_at = None
            self._thread = threading.Thread(target=self._run, args=(refresh,),
                                            daemon=True, name="recommend")
            self._thread.start()
            return True

    def _run(self, refresh: bool) -> None:
        try:
            client = self._make_client()
            self._counts = generate(self.db, client, self.config.cache_dir_path,
                                    self.config.images_dir, refresh=refresh,
                                    progress=self._progress)
        except Exception as exc:  # 后台线程兜底，错误在 status 里可见
            with self._lock:
                self._error = f"{type(exc).__name__}: {exc}"
        finally:
            with self._lock:
                self._phase = "idle"
                self._finished_at = utcnow()

    def _progress(self, done: int, total: int, label: str) -> None:
        with self._lock:
            self._done = done
            self._total = total

    def status(self) -> dict:
        with self._lock:
            running = self._thread is not None and self._thread.is_alive()
            return {
                "running": running,
                "phase": self._phase,
                "done": self._done,
                "total": self._total,
                "movies": self._counts.get("movies", 0),
                "shows": self._counts.get("shows", 0),
                "sources": self._counts.get("sources", 0),
                "started_at": self._started_at,
                "finished_at": self._finished_at,
                "error": self._error,
            }


def _require_item(db: Library, item_type: str, item_id: int):
    """校验 type 并取出条目行：type 不合法 422、条目不存在 404。"""
    if item_type == "movie":
        row = db.get_movie(item_id)
    elif item_type == "episode":
        row = db.get_episode(item_id)
    else:
        raise HTTPException(status_code=422, detail="type 必须是 movie 或 episode")
    if row is None:
        raise HTTPException(status_code=404, detail="条目不存在")
    return row


def _row_value(row, key: str, default=None):
    return row[key] if key in row.keys() else default


def _progress_of(position, duration):
    if position is None or not duration:
        return None
    return max(0.0, min(1.0, position / duration))


def _genres(raw) -> list:
    try:
        data = json.loads(raw or "[]")
    except ValueError:
        return []
    return data if isinstance(data, list) else []


def _movie_payload(row, detail: bool = False) -> dict:
    data = {
        "type": "movie",
        "id": row["id"],
        "href": f"#/movie/{row['id']}",
        "title": row["title"],
        "year": row["year"],
        "status": row["status"],
        "poster": row["poster_cached"],
        "backdrop": row["backdrop_cached"],
        "added_at": row["added_at"],
        "watched": bool(_row_value(row, "watched", 0)),
    }
    if detail:
        position = _row_value(row, "position_sec")
        duration = _row_value(row, "duration_sec")
        data.update({
            "overview": row["overview"],
            "genres": _genres(row["genres"]),
            "rating": row["rating"],
            "runtime_minutes": row["runtime_minutes"],
            "position_sec": position,
            "duration_sec": duration,
            "progress": _progress_of(position, duration),
        })
    return data


def _show_payload(row, detail: bool = False) -> dict:
    data = {
        "type": "show",
        "id": row["id"],
        "href": f"#/show/{row['id']}",
        "title": row["title"],
        "year": row["year"],
        "status": row["status"],
        "poster": row["poster_cached"],
        "backdrop": row["backdrop_cached"],
        "added_at": row["added_at"],
        "episode_count": _row_value(row, "episode_count", 0),
        "watched_count": _row_value(row, "watched_count", 0),
    }
    if detail:
        data.update({
            "overview": row["overview"],
            "genres": _genres(row["genres"]),
            "rating": row["rating"],
            "runtime_minutes": row["runtime_minutes"],
        })
    return data


def _episode_payload(row) -> dict:
    position = _row_value(row, "position_sec")
    duration = _row_value(row, "duration_sec")
    watched = bool(_row_value(row, "watched", 0))
    return {
        "type": "episode",
        "id": row["id"],
        "show_id": row["show_id"],
        "season_number": row["season_number"],
        "episode_number": row["episode_number"],
        "title": row["title"],
        "still": row["still_cached"],
        "runtime_minutes": row["runtime_minutes"],
        "air_date": row["air_date"],
        "missing": bool(row["missing"]),
        "watched": watched,
        "position_sec": position,
        "duration_sec": duration,
        "progress": 1.0 if watched else _progress_of(position, duration),
    }


def _rec_payload(row) -> dict:
    try:
        because = json.loads(row["because"] or "[]")
    except ValueError:
        because = []
    kind = row["kind"]
    tmdb_id = row["tmdb_id"]
    rating = row["vote_average"]
    return {
        "kind": kind,
        "tmdb_id": tmdb_id,
        "title": row["title"],
        "year": row["year"],
        "overview": row["overview"],
        "poster": row["poster_cached"],
        "rating": round(rating, 1) if rating is not None else None,
        "because": because if isinstance(because, list) else [],
        "url": "https://www.themoviedb.org/" + ("movie" if kind == "movie" else "tv")
               + "/" + str(tmdb_id),
    }


class DismissBody(BaseModel):
    kind: str
    tmdb_id: int


def create_app(config: Config | None = None, db: Library | None = None,
               player: Player | None = None, scan_service: ScanService | None = None,
               rec_service: RecService | None = None,
               web_dir: Path | None = None) -> FastAPI:
    """依赖可注入：测试传临时 config/db 和假 launcher 的 Player。"""
    if config is None:
        from .config import load_config
        config = load_config()
    if db is None:
        db = Library(config.db_file)
    if player is None:
        player = Player(db=db, config=config)
    if scan_service is None:
        scan_service = ScanService(config, db)
    if rec_service is None:
        rec_service = RecService(config, db)
    web = Path(web_dir) if web_dir is not None else WEB_DIR
    app = FastAPI(title="HomeCinema", docs_url=None, redoc_url=None)

    @app.get("/")
    def index():
        return FileResponse(web / "index.html")

    @app.get("/api/home")
    def home():
        return {
            "continue_watching": db.continue_watching(20),
            "recently_added": db.recently_added(20),
            "movies": [_movie_payload(r) for r in db.list_movies(sort="added")],
            "shows": [_show_payload(r) for r in db.list_shows(sort="added")],
        }

    @app.get("/api/movies")
    def list_movies(q: str = "", sort: str = "added"):
        return [_movie_payload(r) for r in db.list_movies(q=q, sort=sort)]

    @app.get("/api/shows")
    def list_shows(q: str = "", sort: str = "added"):
        return [_show_payload(r) for r in db.list_shows(q=q, sort=sort)]

    @app.get("/api/movie/{movie_id}")
    def movie_detail(movie_id: int):
        row = db.get_movie(movie_id)
        if row is None:
            raise HTTPException(status_code=404, detail="电影不存在")
        return _movie_payload(row, detail=True)

    @app.get("/api/show/{show_id}")
    def show_detail(show_id: int):
        row = db.get_show(show_id)
        if row is None:
            raise HTTPException(status_code=404, detail="剧集不存在")
        episodes = db.episodes_for_show(show_id)
        season_rows = {r["season_number"]: r for r in db.seasons_for_show(show_id)}
        by_season: dict[int, list] = {}
        for ep in episodes:
            by_season.setdefault(ep["season_number"], []).append(ep)
        seasons = []
        for sn in sorted(by_season):
            srow = season_rows.get(sn)
            name = (srow["name"] if srow and srow["name"] else None) or f"第 {sn} 季"
            seasons.append({
                "season_number": sn,
                "name": name,
                "episodes": [_episode_payload(ep) for ep in by_season[sn]],
            })
        next_up = db.next_up_for_show(show_id)
        return {
            **_show_payload(row, detail=True),
            "seasons": seasons,
            "next_episode": _episode_payload(next_up[0]) if next_up else None,
        }

    @app.post("/api/play")
    def play(body: PlayBody):
        _require_item(db, body.type, body.id)
        try:
            if body.embedded:
                # App 内嵌播放器：不启动 IINA，只把路径/起点/标题交给外壳
                return player.play_embedded(body.type, body.id)
            return player.play(body.type, body.id)
        except PlayerError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @app.post("/api/progress")
    def progress(body: ProgressBody):
        """内嵌播放器回传进度，落库规则与 IINA 监控线程完全一致。"""
        _require_item(db, body.type, body.id)
        player.save_progress(body.type, body.id, body.position, body.duration)
        return {"ok": True}

    @app.get("/api/segments")
    def segments(episode_id: int):
        """调试用：返回该集识别出的片头/片尾记录。"""
        row = db.get_segments(episode_id)
        if row is None:
            raise HTTPException(status_code=404, detail="没有该集的片头片尾记录")
        return dict(row)

    @app.get("/api/next")
    def next_episode(type: str, id: int):
        """同部剧的下一集（季末跨季）；没有下一集返回 null。"""
        if type != "episode":
            raise HTTPException(status_code=422, detail="只支持 type=episode")
        row = _require_item(db, type, id)
        nxt = db.next_episode(row["show_id"], row["season_number"], row["episode_number"])
        return _episode_payload(nxt) if nxt is not None else None

    @app.post("/api/watched")
    def watched(body: WatchedBody):
        if not db.set_watched(body.type, body.id, body.watched):
            raise HTTPException(status_code=404, detail="条目不存在")
        return {"ok": True}

    @app.post("/api/scan")
    def scan():
        started = scan_service.start()
        return {"ok": True, "already_running": not started}

    @app.get("/api/scan/status")
    def scan_status():
        return {**scan_service.status(), "playing": player.active_count(),
                "pid": os.getpid(), "server_started": SERVER_STARTED}

    @app.get("/api/recommendations")
    def recommendations():
        rows = db.list_recommendations()
        return {
            "movies": [_rec_payload(r) for r in rows if r["kind"] == "movie"],
            "shows": [_rec_payload(r) for r in rows if r["kind"] == "tv"],
            "generated_at": db.recommendations_generated_at(),
        }

    @app.post("/api/recommendations/refresh")
    def recommendations_refresh():
        if not config.read_tmdb_key():
            raise HTTPException(status_code=409,
                                detail="没有配置 TMDB API key，无法生成推荐")
        started = rec_service.start(refresh=True)
        return {"ok": True, "already_running": not started}

    @app.get("/api/recommendations/status")
    def recommendations_status():
        return rec_service.status()

    @app.post("/api/recommendations/dismiss")
    def recommendations_dismiss(body: DismissBody):
        if body.kind not in ("movie", "tv"):
            raise HTTPException(status_code=422, detail="kind 必须是 movie 或 tv")
        db.dismiss_recommendation(body.kind, body.tmdb_id)
        return {"ok": True}

    @app.get("/img/{name}")
    def image(name: str):
        images = config.images_dir
        path = (images / name).resolve()
        try:
            path.relative_to(images.resolve())
        except ValueError:
            raise HTTPException(status_code=404, detail="图片不存在")
        if not path.is_file():
            raise HTTPException(status_code=404, detail="图片不存在")
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return FileResponse(path, media_type=media_type)

    app.mount("/static", StaticFiles(directory=web), name="static")
    return app
