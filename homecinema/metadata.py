"""TMDB 元数据：搜索/详情/图片缓存，NFO 与本地海报解析。

HTTP 客户端经构造参数注入（默认 httpx.Client）；测试用假客户端返回固定 JSON。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from .db import Library

_NFO_MAX_BYTES = 2 * 1024 * 1024


class TmdbClient:
    API = "https://api.themoviedb.org/3"
    IMAGE = "https://image.tmdb.org/t/p/"
    # 原图一张 1–5MB，几千张剧照会占好几 GB；按用途取够用的尺寸
    SIZES = {"poster": "w500", "backdrop": "w1280", "still": "w300"}

    def __init__(self, api_key: str, language: str = "zh-CN", http=None,
                 sleep=time.sleep, clock=time.monotonic, min_interval: float = 0.25,
                 timeout: float = 15.0, max_retries: int = 3, backoff_base: float = 0.5):
        self.api_key = api_key
        self.language = language or "zh-CN"
        self._http = http
        self._sleep = sleep
        self._clock = clock
        self._min_interval = min_interval
        self._timeout = timeout
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._last_request = clock() - min_interval

    def _client(self):
        if self._http is None:
            import httpx
            self._http = httpx.Client(timeout=self._timeout)
        return self._http

    def _throttle(self) -> None:
        wait = self._min_interval - (self._clock() - self._last_request)
        if wait > 0:
            self._sleep(wait)
        self._last_request = self._clock()

    def _request(self, url: str, params: dict | None = None):
        """429/5xx/网络错误退避重试最多 max_retries 次；返回最后一次响应。

        网络错误（超时、断线）重试用尽后返回 status_code=0 的占位响应，
        不抛异常——一条超时不能让整轮几百部片的元数据中断。"""
        attempt = 0
        while True:
            self._throttle()
            try:
                resp = self._client().get(url, params=params, timeout=self._timeout)
            except Exception as exc:  # httpx.TransportError 及假客户端抛的同类错误
                resp = _NetworkFailure(exc)
            if resp.status_code in (0, 429) or resp.status_code >= 500:
                attempt += 1
                if attempt > self._max_retries:
                    return resp
                self._sleep(self._backoff_base * (2 ** (attempt - 1)))
                continue
            return resp

    def _get(self, path: str, params: dict | None = None, language: str | None = None):
        full = dict(params or {})
        full["api_key"] = self.api_key
        full["language"] = language or self.language
        resp = self._request(self.API + path, full)
        if resp.status_code != 200:
            return None
        try:
            return resp.json()
        except ValueError:
            return None

    def search_movie(self, title: str, year: int | None = None) -> dict | None:
        # TMDB 的 year 参数匹配任意地区的上映日期，过滤很松（2010 的《告白》会搜出 1981 年的
        # True Confessions 排第一）。所以按「上映年份相差 ≤1 年，或片名完全一致」挑，挑不出就不配。
        tries = [{"primary_release_year": int(year)}, {"year": int(year)}, {}] if year else [{}]
        return self._search("/search/movie", title, year, tries, "release_date",
                            ("title", "original_title"))

    def search_tv(self, title: str, year: int | None = None) -> dict | None:
        tries = [{"first_air_date_year": int(year)}, {}] if year else [{}]
        return self._search("/search/tv", title, year, tries, "first_air_date",
                            ("name", "original_name"))

    def find_by_imdb(self, imdb_id: str, kind: str) -> dict | None:
        """按 IMDb 编号精确查（nfo 里常有 IMDb 链接）。kind: movie / tv。"""
        data = self._get("/find/" + imdb_id, {"external_source": "imdb_id"})
        results = (data or {}).get("movie_results" if kind == "movie" else "tv_results") or []
        return results[0] if results else None

    def _search(self, path, title, year, tries, date_key, title_keys):
        """搜索并挑一个可信的候选；没有可信的就返回 None（宁可不配，不配错）。

        片名比对要用英文结果：zh-CN 搜索返回的 title 是中文译名，跟英文文件夹名永远对不上。
        文件夹名带中文时再加一轮 zh-CN 搜索，比对中文片名。
        可信 = 片名一致且年份差 ≤1（没有年份时只看片名，同名多部取投票数最多的）；
        或者有年份时，年份差 ≤1 且片名高度相似（"Ashes of Time Redux" 这类加了后缀的）。"""
        title = _query_title(title)
        want = _norm_title(title)
        langs = ["en-US"] + (["zh-CN"] if _has_cjk(title) else [])
        seen, cands = set(), []
        for lang in langs:
            for extra in tries:
                data = self._get(path, {"query": title, **extra}, language=lang)
                for r in (data or {}).get("results") or []:
                    key = (lang, r.get("id"))
                    if r.get("id") is not None and key not in seen:
                        seen.add(key)
                        cands.append(r)

        def year_diff(r):
            y = (r.get(date_key) or "")[:4]
            return abs(int(y) - int(year)) if (year and y.isdigit()) else None

        def names(r):
            return {_norm_title(r.get(k)) for k in title_keys} - {""}

        exact = [r for r in cands if want in names(r)]
        votes = lambda r: -(r.get("vote_count") or 0)
        if year:
            good = [r for r in exact if year_diff(r) is not None and year_diff(r) <= 1]
            if good:
                return sorted(good, key=votes)[0]
            close = [r for r in cands if year_diff(r) is not None and year_diff(r) <= 1
                     and (max((_similar(want, n) for n in names(r)), default=0) >= 0.75
                          or any(len(n) >= 4 and len(want) >= 4 and (n in want or want in n)
                                 for n in names(r)))]
            return sorted(close, key=votes)[0] if close else None
        if exact:
            return sorted(exact, key=votes)[0]
        # 没有年份（剧集）：片名是候选的前缀也算（"Misaeng" → "Misaeng: Incomplete Life"）
        prefix = [r for r in cands if len(want) >= 5 and any(n.startswith(want) for n in names(r))]
        return sorted(prefix, key=votes)[0] if prefix else None

    def movie_detail(self, tmdb_id: int, language: str | None = None) -> dict | None:
        return self._get("/movie/" + str(int(tmdb_id)), {}, language=language)

    def tv_detail(self, tmdb_id: int, language: str | None = None) -> dict | None:
        return self._get("/tv/" + str(int(tmdb_id)), {}, language=language)

    def season_detail(self, tv_id: int, season_number: int,
                      language: str | None = None) -> dict | None:
        return self._get("/tv/" + str(int(tv_id)) + "/season/" + str(int(season_number)),
                         {}, language=language)

    def recommendations(self, kind: str, tmdb_id: int) -> list[dict]:
        """TMDB 推荐接口第 1 页；失败/无结果返回空列表。kind: movie / tv。"""
        data = self._get("/" + kind + "/" + str(int(tmdb_id)) + "/recommendations", {})
        results = (data or {}).get("results")
        return results if isinstance(results, list) else []

    def download_image(self, image_path: str, images_dir: Path, kind: str = "poster") -> str | None:
        """下载 TMDB 图片到缓存；文件名 = 尺寸+路径 sha256 前 24 位 + 扩展名。只缓存成功响应。"""
        if not image_path:
            return None
        size = self.SIZES.get(kind, "w500")
        ext = Path(image_path).suffix.lower() or ".jpg"
        name = hashlib.sha256((size + image_path).encode("utf-8")).hexdigest()[:24] + ext
        dest = images_dir / name
        if dest.is_file():
            return name
        resp = self._request(self.IMAGE + size + image_path)
        content = getattr(resp, "content", b"")
        if resp.status_code == 200 and content:
            images_dir.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(content)
            return name
        return None


class _NetworkFailure:
    """网络层异常的占位响应。"""
    status_code = 0
    content = b""

    def __init__(self, exc: Exception):
        self.error = f"{type(exc).__name__}: {exc}"

    def json(self):
        raise ValueError(self.error)


def _safe_parse_xml(text: str):
    """NFO 虽是本地文件仍按不可信处理：拒绝 DTD/实体，限大小。"""
    if len(text) > _NFO_MAX_BYTES:
        return None
    lowered = text.lower()
    if "<!doctype" in lowered or "<!entity" in lowered:
        return None
    try:
        return ET.fromstring(text)
    except ET.ParseError:
        return None


def _extract_tmdbid(text: str) -> int | None:
    """只认影片自己那一层的编号：Kodi/Jellyfin 的 nfo 里每个 <actor> 也带 <tmdbid>，
    取全文第一个会拿到演员的编号（Top Gun 被配成 Reservoir Dogs 就是这么来的）。"""
    root = _safe_parse_xml(text)
    if root is not None:
        for el in list(root):
            tag = el.tag.rsplit("}", 1)[-1].lower()
            value = (el.text or "").strip()
            if not value.isdigit():
                continue
            if tag == "uniqueid" and (el.get("type") or "").lower() == "tmdb":
                return int(value)
            if tag == "tmdbid":
                return int(value)
        return None
    # 不是合法 XML（发布组的文本 nfo）：只有全文仅一个 tmdbid 时才信
    ids = set(re.findall(r"(?is)<tmdbid>\s*(\d+)\s*</tmdbid>", text))
    ids |= set(re.findall(r'(?is)<uniqueid[^>]*type="tmdb"[^>]*>\s*(\d+)\s*</uniqueid>', text))
    return int(ids.pop()) if len(ids) == 1 else None


def _year_close(date: str | None, year) -> bool:
    y = (date or "")[:4]
    if not year or not y.isdigit():
        return True
    return abs(int(y) - int(year)) <= 1


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in str(text or ""))


def _similar(a: str, b: str) -> float:
    from difflib import SequenceMatcher
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return min(len(a), len(b)) / max(len(a), len(b)) + 0.25
    return SequenceMatcher(None, a, b).ratio()


_ROMAN = {"ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6", "vii": "7", "viii": "8"}


def _norm_title(value) -> str:
    """比较片名用：小写、罗马数字续集号转阿拉伯数字、去掉空格和标点、"&" 当 "and"
    （"The Hobbit - X" 与 "The Hobbit: X"、"Swordsman II" 与 "Swordsman 2" 视为相同）。"""
    words = re.findall(r"\w+", str(value or "").lower().replace("&", " and "))
    return "".join(_ROMAN.get(w, w) for w in words)


def _query_title(title: str) -> str:
    """搜索用的片名：去掉括号注记，"The Office (US)" 搜 "The Office"。"""
    return re.sub(r"\s*\([^)]*\)\s*", " ", title).strip() or title


_IMDB_ID = re.compile(r"\b(tt\d{7,9})\b")


def parse_nfo_imdbid(folder: str | Path) -> str | None:
    """同目录 .nfo 里的 IMDb 编号（含发布组 nfo 里的 IMDb 链接）。"""
    folder = Path(folder)
    if not folder.is_dir():
        return None
    for nfo in sorted(folder.glob("*.nfo"), key=lambda p: p.name):
        try:
            if nfo.stat().st_size > _NFO_MAX_BYTES:
                continue
            m = _IMDB_ID.search(nfo.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if m:
            return m.group(1)
    return None


def parse_nfo_tmdbid(folder: str | Path) -> int | None:
    """同目录任一 .nfo 里的 <tmdbid> 或 <uniqueid type="tmdb">。"""
    folder = Path(folder)
    if not folder.is_dir():
        return None
    for nfo in sorted(folder.glob("*.nfo"), key=lambda p: p.name):
        try:
            if nfo.stat().st_size > _NFO_MAX_BYTES:
                continue
            text = nfo.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        tmdb_id = _extract_tmdbid(text)
        if tmdb_id is not None:
            return tmdb_id
    return None


def find_local_poster(folder: str | Path, video_name: str = "") -> Path | None:
    """poster.jpg/png、folder.jpg/png 或与视频同名的 .jpg/.png（大小写不敏感）。"""
    folder = Path(folder)
    if not folder.is_dir():
        return None
    try:
        names = {p.name.lower(): p for p in folder.iterdir() if p.is_file()}
    except OSError:
        return None
    for cand in ("poster.jpg", "poster.png", "folder.jpg", "folder.png"):
        if cand in names:
            return names[cand]
    stem = Path(video_name).stem.lower() if video_name else ""
    if stem:
        for ext in (".jpg", ".jpeg", ".png"):
            hit = names.get(stem + ext)
            if hit:
                return hit
    return None


def cache_local_image(src: Path, images_dir: Path) -> str | None:
    """把本地海报拷进缓存目录，命名 local_<路径 hash>；返回缓存文件名。"""
    try:
        data = src.read_bytes()
    except OSError:
        return None
    if not data:
        return None
    ext = src.suffix.lower() or ".jpg"
    name = "local_" + hashlib.sha256(str(src).encode("utf-8")).hexdigest()[:24] + ext
    dest = images_dir / name
    if not dest.is_file() or dest.stat().st_size != len(data):
        images_dir.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    return name


class MetadataService:
    def __init__(self, db: Library, client: TmdbClient, images_dir: Path, progress=None,
                 refresh: bool = False, overrides: dict | None = None):
        # refresh=True：已匹配的也重新匹配（匹配规则改了之后纠错用），不沿用库里旧的 tmdb_id
        self.refresh = refresh
        self.overrides = overrides or {"movie": {}, "tv": {}}
        self.db = db
        self.client = client
        self.images_dir = Path(images_dir)
        self._progress = progress

    def _report(self, done: int, total: int, label: str) -> None:
        if self._progress:
            self._progress(done, total, label)

    def enrich_all(self) -> dict:
        movies = self.db.movies_for_metadata(include_matched=self.refresh)
        shows = self.db.shows_for_metadata(include_matched=self.refresh)
        total = len(movies) + len(shows)
        stats = {"matched": 0, "unmatched": 0, "total": total}
        done = 0
        stats["failed"] = 0
        jobs = [(self.enrich_movie, r) for r in movies] + [(self.enrich_show, r) for r in shows]
        for fn, row in jobs:
            try:
                ok = fn(row)
                stats["matched" if ok else "unmatched"] += 1
                label = row["title"]
            except Exception as exc:
                # 单条出错不中断整轮；状态不改，下次补元数据会重试这一条
                stats["failed"] += 1
                label = f"{row['title']}（出错：{type(exc).__name__}: {exc}）"
            done += 1
            self._report(done, total, label)
        return stats

    # ---------- 电影 ----------

    def enrich_movie(self, row) -> bool:
        folder = Path(row["folder"])
        fields: dict = {}
        local = find_local_poster(folder, row["video_name"])
        if local:
            cached = cache_local_image(local, self.images_dir)
            if cached:
                fields["poster_local"] = str(local)
                fields["poster_cached"] = cached
        tmdb_id = (self.overrides.get("movie", {}).get(unicodedata.normalize("NFC", folder.name)) or parse_nfo_tmdbid(folder)
                   or (None if self.refresh else row["tmdb_id"]))
        detail = None
        if tmdb_id is None:
            imdb = parse_nfo_imdbid(folder)
            found = self.client.find_by_imdb(imdb, "movie") if imdb else None
            # 发布组 nfo 常写原版的 IMDb 号（Ashes of Time Redux 2008 写的是 1994 原版），年份对不上就不信
            if found and not _year_close(found.get("release_date"), row["year"]):
                found = None
            if not found:
                found = self.client.search_movie(row["title"], row["year"])
            if found:
                tmdb_id = found.get("id")
        if tmdb_id is not None:
            detail = self.client.movie_detail(tmdb_id)
            if detail and not (detail.get("overview") or "").strip():
                en = self.client.movie_detail(tmdb_id, language="en-US")
                if en and (en.get("overview") or "").strip():
                    detail["overview"] = en["overview"]
        if not detail:
            # 清掉旧匹配留下的简介和图片，免得重匹配失败后还挂着别的片的海报
            self.db.update_movie_metadata(row["id"], {
                "status": "unmatched", "tmdb_id": None, "overview": "", "genres": [],
                "rating": None, "runtime_minutes": None, "poster_path": None,
                "backdrop_path": None, "backdrop_cached": None,
                "poster_cached": fields.get("poster_cached")})
            return False
        fields.update({
            "tmdb_id": detail.get("id"),
            "overview": detail.get("overview") or "",
            "genres": [g.get("name") for g in (detail.get("genres") or []) if g.get("name")],
            "rating": detail.get("vote_average"),
            "runtime_minutes": detail.get("runtime"),
            "poster_path": detail.get("poster_path"),
            "backdrop_path": detail.get("backdrop_path"),
            "status": "matched",
        })
        if not local and detail.get("poster_path"):
            cached = self.client.download_image(detail["poster_path"], self.images_dir)
            if cached:
                fields["poster_cached"] = cached
        if detail.get("backdrop_path"):
            cached = self.client.download_image(detail["backdrop_path"], self.images_dir, "backdrop")
            if cached:
                fields["backdrop_cached"] = cached
        self.db.update_movie_metadata(row["id"], fields)
        return True

    # ---------- 剧集 ----------

    def enrich_show(self, row) -> bool:
        folder = Path(row["folder"])
        fields: dict = {}
        local = find_local_poster(folder)
        if local:
            cached = cache_local_image(local, self.images_dir)
            if cached:
                fields["poster_local"] = str(local)
                fields["poster_cached"] = cached
        tmdb_id = (self.overrides.get("tv", {}).get(unicodedata.normalize("NFC", folder.name)) or parse_nfo_tmdbid(folder)
                   or (None if self.refresh else row["tmdb_id"]))
        detail = None
        if tmdb_id is None:
            imdb = parse_nfo_imdbid(folder)
            found = self.client.find_by_imdb(imdb, "tv") if imdb else None
            if not found:
                found = self.client.search_tv(row["title"], row["year"])
            if found:
                tmdb_id = found.get("id")
        if tmdb_id is not None:
            detail = self.client.tv_detail(tmdb_id)
            if detail and not (detail.get("overview") or "").strip():
                en = self.client.tv_detail(tmdb_id, language="en-US")
                if en and (en.get("overview") or "").strip():
                    detail["overview"] = en["overview"]
        if not detail:
            # 清掉旧匹配留下的简介和图片，免得重匹配失败后还挂着别的片的海报
            self.db.update_show_metadata(row["id"], {
                "status": "unmatched", "tmdb_id": None, "overview": "", "genres": [],
                "rating": None, "runtime_minutes": None, "poster_path": None,
                "backdrop_path": None, "backdrop_cached": None,
                "poster_cached": fields.get("poster_cached")})
            return False
        episode_runtime = (detail.get("episode_run_time") or [None])
        fields.update({
            "tmdb_id": detail.get("id"),
            "overview": detail.get("overview") or "",
            "genres": [g.get("name") for g in (detail.get("genres") or []) if g.get("name")],
            "rating": detail.get("vote_average"),
            "runtime_minutes": episode_runtime[0],
            "poster_path": detail.get("poster_path"),
            "backdrop_path": detail.get("backdrop_path"),
            "status": "matched",
        })
        if not local and detail.get("poster_path"):
            cached = self.client.download_image(detail["poster_path"], self.images_dir)
            if cached:
                fields["poster_cached"] = cached
        if detail.get("backdrop_path"):
            cached = self.client.download_image(detail["backdrop_path"], self.images_dir, "backdrop")
            if cached:
                fields["backdrop_cached"] = cached
        self.db.update_show_metadata(row["id"], fields)

        show_id = row["id"]
        detail_id = detail.get("id")
        for s in detail.get("seasons") or []:
            sn = s.get("season_number")
            if sn is None:
                continue
            self.db.upsert_season(show_id, sn, name=s.get("name"),
                                  episode_count=s.get("episode_count"),
                                  poster_path=s.get("poster_path"))
        local_seasons = sorted({e["season_number"] for e in self.db.episodes_for_show(show_id)})
        for sn in local_seasons:
            sdata = self.client.season_detail(detail_id, sn)
            if not sdata:
                continue
            self.db.upsert_season(show_id, sn, name=sdata.get("name"))
            by_number = {e.get("episode_number"): e for e in (sdata.get("episodes") or [])}
            for ep in self.db.episodes_for_show(show_id):
                if ep["season_number"] != sn:
                    continue
                t = by_number.get(ep["episode_number"])
                if not t:
                    continue
                ep_fields = {
                    "title": t.get("name") or ep["title"],
                    "overview": t.get("overview") or "",
                    "still_path": t.get("still_path"),
                    "runtime_minutes": t.get("runtime"),
                    "air_date": t.get("air_date"),
                    "tmdb_id": t.get("id"),
                }
                if not ep["missing"] and t.get("still_path"):
                    cached = self.client.download_image(t["still_path"], self.images_dir, "still")
                    if cached:
                        ep_fields["still_cached"] = cached
                self.db.update_episode_metadata(ep["id"], ep_fields)
        return True
