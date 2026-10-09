"""FastAPI 服务：浏览、搜索、播放、进度、扫描。只监听 127.0.0.1（见 __main__）。"""

from __future__ import annotations

import json
import mimetypes
import os
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import Config
from .db import Library, _default_version_key, _version_label, utcnow
from .extras import refresh_airing, run_extras
from .metadata import MetadataService, TmdbClient
from .player import Player, PlayerError
from .recommend import generate
from .scanner import run_scan
from . import subtitles as subs_mod

WEB_DIR = Path(__file__).resolve().parent / "web"
# App 外壳据此判断现有服务是不是在代码更新之前起的（是就重启它，免得沿用旧代码）
SERVER_STARTED = time.time()

# audit N01：射手网的传输层故障（断网/超时/连接重置）与「上游返回了错误」同属上游不可用。
# httpx 是按需 import 的，这里把它的 TransportError 连同 socket 层 OSError 一起归到 502。
_ASSRT_TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (OSError,)
try:  # pragma: no cover - 取决于运行环境是否装了 httpx
    import httpx as _httpx

    _ASSRT_TRANSPORT_ERRORS = (OSError, _httpx.TransportError)
except ImportError:  # pragma: no cover
    pass


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
                # 与 CLI fetch-metadata 一致：手工匹配规则 overrides.toml 也要生效
                service = MetadataService(self.db, client, self.config.images_dir,
                                          progress=self._meta_progress,
                                          overrides=self.config.read_overrides())
                service.enrich_all()
                with self._lock:
                    self._phase = "extras"
                # 新入库/新匹配的片补演职员、合集、播出信息
                run_extras(self.db, client, self.config.images_dir, progress=lambda *a, **k: None)
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


class AiringService:
    """追剧日历刷新（后台）：只重拉没完结的剧的播出信息。"""

    def __init__(self, config: Config, db: Library, client_factory=None):
        self.config = config
        self.db = db
        self._client_factory = client_factory
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._done = 0
        self._total = 0
        self._error: str | None = None

    def start(self) -> bool:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._done = self._total = 0
            self._error = None
            self._thread = threading.Thread(target=self._run, daemon=True, name="airing")
            self._thread.start()
            return True

    def _run(self) -> None:
        try:
            if self._client_factory is not None:
                client = self._client_factory()
            else:
                key = self.config.read_tmdb_key()
                if not key:
                    raise RuntimeError("没有配置 TMDB API key")
                client = TmdbClient(key, self.config.tmdb_language)
            refresh_airing(self.db, client, self.config.images_dir, progress=self._progress)
        except Exception as exc:  # 后台线程兜底
            with self._lock:
                self._error = f"{type(exc).__name__}: {exc}"

    def _progress(self, done: int, total: int, label: str) -> None:
        with self._lock:
            self._done, self._total = done, total

    def status(self) -> dict:
        with self._lock:
            running = self._thread is not None and self._thread.is_alive()
            return {"running": running, "done": self._done, "total": self._total,
                    "error": self._error}


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


def _movie_payload(row, detail: bool = False, version_count: int = 1,
                   version_ids: list[int] | None = None) -> dict:
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
        "genres": _genres(row["genres"]),
        "rating": row["rating"],
        "video_height": _row_value(row, "video_height"),
        "version_count": version_count,
        "version_ids": list(version_ids) if version_ids else [row["id"]],
        "in_progress": bool((_row_value(row, "position_sec") or 0) >= 30 and not _row_value(row, "watched", 0)),
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
        "genres": _genres(row["genres"]),
        "rating": row["rating"],
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


def _movie_version_payload(v, default_id: int) -> dict:
    position = _row_value(v, "position_sec")
    duration = _row_value(v, "duration_sec")
    height = _row_value(v, "video_height")
    return {
        "id": v["id"],
        "name": _version_label(v["folder"], v["video_name"], v["title"], v["ext"], height),
        "video_height": height,
        "ext": v["ext"],
        "size": v["size"],
        "watched": bool(_row_value(v, "watched", 0)),
        "position_sec": position,
        "duration_sec": duration,
        "progress": _progress_of(position, duration),
        "played_at": _row_value(v, "played_at"),
        "is_default": v["id"] == default_id,
    }


def _people_payload(rows, roles: tuple[str, ...]) -> list[dict]:
    return [{"id": r["id"], "name": r["name"], "profile": r["profile_cached"],
             "character": r["character"]} for r in rows if r["role"] in roles]


def _res_bucket(height) -> str | None:
    if not height:
        return None
    if height >= 1600:
        return "4k"
    if height >= 900:
        return "1080"
    if height >= 650:
        return "720"
    return "sd"


def _filter_items(items: list[dict], genre: str, min_rating: float, watched: str,
                  fav: bool, res: str, favs: set[int]) -> list[dict]:
    out = []
    for it in items:
        # 卡片代表整部电影：组内任一版本被收藏，这张卡就算收藏
        it["favorite"] = bool(set(it.get("version_ids") or [it["id"]]) & favs)
        if genre and genre not in (it.get("genres") or []):
            continue
        if min_rating and (it.get("rating") or 0) < min_rating:
            continue
        if fav and not it["favorite"]:
            continue
        if res and _res_bucket(it.get("video_height")) != res:
            continue
        if watched:
            if it["type"] == "movie":
                state = "watched" if it["watched"] else ("progress" if it.get("in_progress") else "unwatched")
            else:
                n, w = it.get("episode_count") or 0, it.get("watched_count") or 0
                state = "watched" if n and w >= n else ("progress" if w else "unwatched")
            if state != watched:
                continue
        out.append(it)
    return out


def _air_payload(raw):
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


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


class FavoriteBody(BaseModel):
    type: str
    id: int
    favorite: bool


class SubtitleBody(BaseModel):
    type: str
    id: int
    sub_id: int | None = None
    name: str | None = None


class DismissBody(BaseModel):
    kind: str
    tmdb_id: int


def create_app(config: Config | None = None, db: Library | None = None,
               player: Player | None = None, scan_service: ScanService | None = None,
               rec_service: RecService | None = None,
               web_dir: Path | None = None,
               airing_service: AiringService | None = None) -> FastAPI:
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
    if airing_service is None:
        airing_service = AiringService(config, db)
    web = Path(web_dir) if web_dir is not None else WEB_DIR
    app = FastAPI(title="HomeCinema", docs_url=None, redoc_url=None)

    @app.get("/")
    def index():
        # app.js / app.css 带上修改时间当版本号：App 里的 WKWebView 会缓存静态文件，改了前端也看不到
        html = (web / "index.html").read_text(encoding="utf-8")
        for name in ("app.js", "app.css"):
            try:
                ver = int((web / name).stat().st_mtime)
            except OSError:
                continue
            html = html.replace(f"/static/{name}\"", f"/static/{name}?v={ver}\"")
        return HTMLResponse(html)

    @app.get("/api/home")
    def home():
        return {
            "continue_watching": db.continue_watching(20),
            "recently_added": db.recently_added(20),
            "movies": [_movie_payload(g["row"], version_count=g["count"],
                                      version_ids=g["version_ids"])
                       for g in db.list_movie_groups(sort="added")],
            "shows": [_show_payload(r) for r in db.list_shows(sort="added")],
        }

    @app.get("/api/movies")
    def list_movies(q: str = "", sort: str = "added", genre: str = "", min_rating: float = 0,
                    watched: str = "", fav: bool = False, res: str = ""):
        groups = db.list_movie_groups(q=q, sort="title" if sort == "rating" else sort)
        items = [_movie_payload(g["row"], version_count=g["count"],
                                version_ids=g["version_ids"]) for g in groups]
        if sort == "rating":
            items.sort(key=lambda x: -(x["rating"] or 0))
        return _filter_items(items, genre, min_rating, watched, fav, res, db.favorite_ids("movie"))

    @app.get("/api/shows")
    def list_shows(q: str = "", sort: str = "added", genre: str = "", min_rating: float = 0,
                   watched: str = "", fav: bool = False):
        if sort == "rating":
            items = sorted((_show_payload(r) for r in db.list_shows(q=q)),
                           key=lambda x: -(x["rating"] or 0))
        else:
            items = [_show_payload(r) for r in db.list_shows(q=q, sort=sort)]
        return _filter_items(items, genre, min_rating, watched, fav, "", db.favorite_ids("show"))

    @app.get("/api/movie/{movie_id}")
    def movie_detail(movie_id: int):
        row = db.get_movie(movie_id)
        if row is None:
            raise HTTPException(status_code=404, detail="电影不存在")
        credits = db.credits_for("movie", movie_id)
        coll = None
        if _row_value(row, "collection_id"):
            c = db.get_collection(row["collection_id"])
            if c is not None:
                coll = {"id": c["id"], "name": c["name"],
                        "owned": len(db.movies_in_collection(c["id"]))}
        versions = db.movie_versions(movie_id)
        default_id = max(versions, key=_default_version_key)["id"] if versions else movie_id
        # audit N02：收藏开关代表「这部电影」而不是当前查看的这个文件——与列表卡片
        # （_filter_items：组内任一版本被收藏即收藏）同口径，否则详情页显示未收藏、
        # 一点就把另一个版本也收藏了，状态与用户操作相反。
        # 只改 favorite 的取值，不动 version_ids 等既有字段（对外接口不变）。
        favs = db.favorite_ids("movie")
        group_ids = {v["id"] for v in versions} or {movie_id}
        return {**_movie_payload(row, detail=True, version_count=len(versions)),
                "versions": [_movie_version_payload(v, default_id) for v in versions],
                "default_version_id": default_id,
                "directors": _people_payload(credits, ("director",)),
                "cast": _people_payload(credits, ("cast",)),
                "collection": coll,
                "favorite": bool(group_ids & favs)}

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
        present = [ep for ep in episodes if not ep["missing"]]
        return {
            **_show_payload(row, detail=True),
            # get_show 是 SELECT * FROM shows，没有列表接口子查询算的这两列
            "episode_count": len(present),
            "watched_count": sum(1 for ep in present if _row_value(ep, "watched", 0)),
            "seasons": seasons,
            "next_episode": _episode_payload(next_up[0]) if next_up else None,
            "creators": _people_payload(db.credits_for("show", show_id), ("creator",)),
            "cast": _people_payload(db.credits_for("show", show_id), ("cast",)),
            "favorite": show_id in db.favorite_ids("show"),
            "tmdb_status": _row_value(row, "tmdb_status"),
            "next_air": _air_payload(_row_value(row, "next_air")),
            "missing_episodes": db.missing_episodes(show_id),
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
        if body.type not in ("movie", "episode"):
            raise HTTPException(status_code=422, detail="type 必须是 movie 或 episode")
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

    @app.get("/api/person/{person_id}")
    def person(person_id: int):
        p = db.get_person(person_id)
        if p is None:
            raise HTTPException(status_code=404, detail="没有这个人")
        made, acted = db.works_of_person(person_id)

        def item(kind, row, character):
            data = _movie_payload(row) if kind == "movie" else _show_payload(row)
            data["character"] = character
            return data

        key = lambda d: (d["year"] is None, -(d["year"] or 0))
        return {"id": p["id"], "name": p["name"], "profile": p["profile_cached"],
                "made": sorted((item(*m) for m in made), key=key),
                "acted": sorted((item(*a) for a in acted), key=key),
                "url": "https://www.themoviedb.org/person/" + str(p["id"])}

    @app.get("/api/people")
    def people(min_count: int = 1):
        return [p for p in db.people_by_role() if p["count"] >= min_count]

    @app.get("/api/collections")
    def collections():
        return db.list_collections()

    @app.get("/api/collection/{coll_id}")
    def collection(coll_id: int):
        c = db.get_collection(coll_id)
        if c is None:
            raise HTTPException(status_code=404, detail="没有这个合集")
        owned = [_movie_payload(r) for r in db.movies_in_collection(coll_id)]
        have = {r["tmdb_id"] for r in db.movies_in_collection(coll_id)}
        try:
            parts = json.loads(c["parts"]) if _row_value(c, "parts") else []
        except ValueError:
            parts = []
        missing = [{**p, "url": "https://www.themoviedb.org/movie/" + str(p["tmdb_id"])}
                   for p in parts if p.get("tmdb_id") not in have]
        return {"id": c["id"], "name": c["name"], "poster": c["poster_cached"],
                "movies": owned, "missing": missing}

    @app.post("/api/favorite")
    def favorite(body: FavoriteBody):
        if body.type not in ("movie", "show"):
            raise HTTPException(status_code=422, detail="type 必须是 movie 或 show")
        if not db.set_favorite(body.type, body.id, body.favorite):
            raise HTTPException(status_code=404, detail="条目不存在")
        return {"ok": True}

    @app.get("/api/calendar")
    def calendar():
        upcoming, new_aired, gaps = [], [], []
        for r in db.list_shows():
            show = {"id": r["id"], "title": r["title"], "poster": r["poster_cached"],
                    "href": f"#/show/{r['id']}"}
            nxt = _air_payload(_row_value(r, "next_air"))
            if nxt:
                upcoming.append({**show, **nxt})
            last = _air_payload(_row_value(r, "last_air"))
            local = db.latest_local_episode(r["id"])
            if last and local and last.get("season") and last.get("episode") \
                    and (last["season"], last["episode"]) > local:
                new_aired.append({**show, **last, "local_season": local[0], "local_episode": local[1]})
            missing = db.missing_episodes(r["id"])
            if missing:
                gaps.append({**show, "missing": missing})
        upcoming.sort(key=lambda x: x["air_date"])
        new_aired.sort(key=lambda x: x["air_date"], reverse=True)
        return {"upcoming": upcoming, "new_aired": new_aired, "gaps": gaps,
                "refreshed_at": db.airing_refreshed_at()}

    @app.post("/api/calendar/refresh")
    def calendar_refresh():
        if not config.read_tmdb_key():
            raise HTTPException(status_code=409, detail="没有配置 TMDB API key")
        started = airing_service.start()
        return {"ok": True, "already_running": not started}

    @app.get("/api/calendar/status")
    def calendar_status():
        return airing_service.status()

    # ---------- 中文字幕 ----------

    def _sub_target(item_type: str, item_id: int):
        row = _require_item(db, item_type, item_id)
        video = os.path.splitext(row["video_name"])[0]  # video_name 是带扩展名的文件名
        if item_type == "movie":
            return row, video, row["title"], row["year"], True
        return row, video, f"{row['show_title']} S{row['season_number']:02d}E{row['episode_number']:02d}", None, False

    def _assrt():
        token = config.read_assrt_token()
        if not token:
            raise HTTPException(status_code=409, detail="没有配置射手网 token（~/.config/assrt/token.txt）")
        return subs_mod.AssrtClient(token)

    @app.get("/api/subtitles")
    def subtitles(type: str, id: int):
        row, *_rest, whole = _sub_target(type, id)
        status = subs_mod.embedded_status(row["path"], whole_folder=whole)
        downloaded = [os.path.basename(p) for p in db.subtitles_for(type, id)]
        return {**status, "downloaded": downloaded, "has_zh": status["has_zh"] or bool(downloaded),
                "search_enabled": bool(config.read_assrt_token())}

    @app.get("/api/subtitles/search")
    def subtitles_search(type: str, id: int):
        _row, video_name, title, year, _whole = _sub_target(type, id)
        try:
            return {"results": subs_mod.search_candidates(_assrt(), video_name, title, year)}
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc))
        except _ASSRT_TRANSPORT_ERRORS as exc:  # audit N01
            # 断网/超时/连接重置也是「上游不可用」，该走同一条 502 通道，
            # 否则最常见的故障形态直接炸成 500 Internal Server Error。
            raise HTTPException(status_code=502, detail=f"射手网连接失败：{exc}")

    @app.post("/api/subtitles/download")
    def subtitles_download(body: SubtitleBody):
        _row, video_name, _t, _y, _w = _sub_target(body.type, body.id)
        if not body.sub_id:
            raise HTTPException(status_code=422, detail="缺 sub_id")
        try:
            path = subs_mod.download_best_file(_assrt(), body.sub_id,
                                               config.subs_dir / f"{body.type}-{body.id}", video_name)
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc))
        except _ASSRT_TRANSPORT_ERRORS as exc:  # audit N01
            raise HTTPException(status_code=502, detail=f"射手网连接失败：{exc}")
        db.add_subtitle(body.type, body.id, str(path), body.sub_id)
        return {"ok": True, "name": path.name}

    @app.post("/api/subtitles/remove")
    def subtitles_remove(body: SubtitleBody):
        _sub_target(body.type, body.id)
        for p in db.subtitles_for(body.type, body.id):
            if os.path.basename(p) == body.name:
                db.remove_subtitle(body.type, body.id, p)
                return {"ok": True}
        raise HTTPException(status_code=404, detail="没有这个字幕")

    @app.get("/img/{name}")
    def image(name: str):
        images = config.images_dir
        try:
            path = (images / name).resolve()  # 含空字节等非法名会抛 ValueError
            path.relative_to(images.resolve())
        except (ValueError, OSError):
            raise HTTPException(status_code=404, detail="图片不存在")
        if not path.is_file():
            raise HTTPException(status_code=404, detail="图片不存在")
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return FileResponse(path, media_type=media_type)

    app.mount("/static", StaticFiles(directory=web), name="static")
    return app
