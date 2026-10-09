"""社区片头片尾库：TheIntroDB（按 TMDB 编号 + 片长认版本）优先，没有再查 IntroDB（按 IMDb 编号）。

两家都免费、匿名读（Infuse 的跳过片头片尾也用这两家）。结果按集存进 community_segments，
不直接改 segments；怎么合并见 tools/merge_segments.py。

HTTP 客户端经参数注入（默认 httpx.Client）；测试传假客户端。
"""

from __future__ import annotations

import json
import os
import time

from .db import Library

THEINTRODB = "https://api.theintrodb.org/v3/media"
INTRODB = "https://api.introdb.app/segments"
MIN_INTERVAL = 0.4   # TheIntroDB 响应头 x-ratelimit-limit: 30（每 10 秒）；IntroDB 只写「合理使用」
USER_AGENT = "HomeCinema (personal local media library)"


class QuotaExceeded(RuntimeError):
    """匿名额度用完（TheIntroDB 429 usage_limit_exceeded，按天重置）。"""


class CommunityClient:
    def __init__(self, http=None, sleep=time.sleep, clock=time.monotonic,
                 min_interval: float = MIN_INTERVAL, max_retries: int = 3,
                 theintrodb_key: str | None = None):
        self._http = http
        self._theintrodb_key = theintrodb_key
        self._sleep = sleep
        self._clock = clock
        self._min_interval = min_interval
        self._max_retries = max_retries
        self._last = clock() - min_interval

    def _client(self):
        if self._http is None:
            import httpx
            self._http = httpx.Client(timeout=15.0, headers={"User-Agent": USER_AGENT})
        return self._http

    def _get(self, url: str, params: dict, headers: dict | None = None) -> dict | None:
        """200 返回 JSON；404/400（没有这一集）返回 None；429/5xx/网络错误退避重试，用尽抛 RuntimeError。"""
        for attempt in range(self._max_retries + 1):
            wait = self._min_interval - (self._clock() - self._last)
            if wait > 0:
                self._sleep(wait)
            self._last = self._clock()
            try:
                resp = self._client().get(url, params=params, headers=headers) if headers \
                    else self._client().get(url, params=params)
                code = resp.status_code
            except Exception as exc:  # 网络错误
                code, resp = 0, exc
            if code == 200:
                try:
                    return resp.json()
                except ValueError:
                    # 10-05 审计 F01：200 但不是 JSON（网关错误页/截断）是链路异常，按可重试处理，
                    # 不能当「没有这一集」落库成永久的 none
                    code = -1
            if code in (400, 404):
                return None
            if code == 429:
                try:
                    body = resp.json()
                except Exception:
                    body = {}
                if isinstance(body, dict) and body.get("code") == "usage_limit_exceeded":
                    raise QuotaExceeded(f"{url} 额度用完，{body.get('retry_after', '')}后重置")
            if attempt < self._max_retries:
                self._sleep(2.0 * (2 ** attempt))
        raise RuntimeError(f"{url} 请求失败（{code}）")

    def theintrodb(self, tmdb_id: int, season: int, episode: int,
                   duration_sec: float | None) -> dict | None:
        params = {"tmdb_id": int(tmdb_id), "season": int(season), "episode": int(episode)}
        if duration_sec:
            params["duration_ms"] = int(duration_sec * 1000)
        # 带 key 有单独一份每日额度（约 1000 次，比匿名约 400 次多）
        headers = {"Authorization": "Bearer " + self._theintrodb_key} if self._theintrodb_key else None
        data = self._get(THEINTRODB, params, headers)
        if not data:
            return None
        if not isinstance(data, dict):  # 10-05 审计 F04：顶层形状不对按查询失败处理
            raise RuntimeError(f"{THEINTRODB} 响应体不是对象")
        if "error" in data:
            return None
        seg = {}
        _first_range(seg, "intro", data.get("intro"))
        _first_range(seg, "recap", data.get("recap"))
        credits = [c for c in (data.get("credits") or []) if isinstance(c, dict)]  # 10-05 审计 F04：跳过 null 项
        if credits and credits[0].get("start_ms") is not None:
            seg["credits_start"] = credits[0]["start_ms"] / 1000.0
            end = credits[0].get("end_ms")
            seg["credits_end"] = end / 1000.0 if end is not None else None
        preview = [p for p in (data.get("preview") or []) if isinstance(p, dict)]
        if preview and preview[0].get("start_ms") is not None:
            seg["preview_start"] = preview[0]["start_ms"] / 1000.0
        return seg or None

    def introdb(self, imdb_id: str, season: int, episode: int) -> dict | None:
        data = self._get(INTRODB, {"imdb_id": imdb_id, "season": int(season),
                                   "episode": int(episode)})
        if not data:
            return None
        if not isinstance(data, dict):
            raise RuntimeError(f"{INTRODB} 响应体不是对象")
        seg = {}
        for key, name in (("intro", "intro"), ("recap", "recap"), ("outro", "credits")):
            item = data.get(key)
            if isinstance(item, dict) and item.get("end_ms") is not None:
                start = item.get("start_ms")
                seg[name + "_start"] = start / 1000.0 if start is not None else 0.0
                seg[name + "_end"] = item["end_ms"] / 1000.0
        return seg or None


def _first_range(seg: dict, name: str, items) -> None:
    """TheIntroDB 的 intro/recap 是列表，start 可以是 null（表示从 0 开始）。"""
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list) or not items:
        return
    first = items[0]
    if not isinstance(first, dict) or first.get("end_ms") is None:
        return
    start = first.get("start_ms")
    seg[name + "_start"] = start / 1000.0 if start is not None else 0.0
    seg[name + "_end"] = first["end_ms"] / 1000.0


def _duration(ep, ocr_cache_dir: str) -> float | None:
    """片长：文字识别缓存里有且与源文件指纹相符就用（不用再读硬盘），否则 ffprobe。
    指纹校验与 ocr_credits.load_or_extract 同一规则（Kimi-E-2）：换源后旧 duration
    不得发给 TheIntroDB 做版本匹配。"""
    from .ocr_credits import cache_matches
    cache = os.path.join(ocr_cache_dir, f"{ep['id']}.json")
    if os.path.exists(cache):
        try:
            with open(cache, encoding="utf-8") as fh:
                cached = json.load(fh)
            try:
                st = os.stat(ep["path"])
            except OSError:
                st = None
            if cache_matches(cached, st):
                return float(cached["duration"])
        except (ValueError, KeyError, TypeError, OSError):
            pass
    from .detect import probe_duration
    try:
        return probe_duration(ep["path"])
    except Exception:
        return None


def run_community(db: Library, client: CommunityClient, tmdb=None, ocr_cache_dir: str = "",
                  refresh: bool = False, progress=print, retry_none: bool = False,
                  limit: int | None = None) -> dict:
    """逐集查社区库并落库（每集查完立即写，中断后重跑跳过已查的）。
    tmdb：TmdbClient，用来给剧补 IMDb 编号（IntroDB 只认 IMDb）；不给就只查 TheIntroDB。
    retry_none=True 时只重查库里记为 none 的集（按 fetched_at 从旧到新，可 limit），
    额度一用完就停止本轮；refresh 与 retry_none 互斥。"""
    if refresh and retry_none:
        raise ValueError("refresh 与 retry_none 互斥")
    episodes = db.episodes_for_community(refresh=refresh,
                                          only_status="none" if retry_none else None,
                                          limit=limit)
    imdb_by_show: dict[int, str | None] = {}
    stats = {"total": len(episodes), "found": 0, "none": 0, "error": 0}
    processed = 0
    theintrodb_ok = True
    stopped = False
    for i, ep in enumerate(episodes, 1):
        show_id = ep["show_id"]
        if show_id not in imdb_by_show:
            imdb = ep["show_imdb_id"]
            if not imdb and tmdb is not None:
                imdb = (tmdb.external_ids("tv", ep["show_tmdb_id"]) or {}).get("imdb_id") or None
                db.set_show_imdb(show_id, imdb)
            imdb_by_show[show_id] = imdb
        try:
            seg, provider = None, None
            if theintrodb_ok:
                try:
                    seg = client.theintrodb(ep["show_tmdb_id"], ep["season_number"], ep["episode_number"],
                                            _duration(ep, ocr_cache_dir))
                    provider = "theintrodb" if seg else None
                except QuotaExceeded as exc:
                    theintrodb_ok = False
                    progress(f"TheIntroDB {exc}；本轮之后只查 IntroDB", flush=True)
            if not seg and imdb_by_show[show_id]:
                seg = client.introdb(imdb_by_show[show_id], ep["season_number"], ep["episode_number"])
                provider = "introdb" if seg else None
        except RuntimeError as exc:
            # 网络/限速失败不落库，下次重跑再查
            stats["error"] += 1
            progress(f"[{i}/{len(episodes)}] {ep['show_title']} S{ep['season_number']:02d}E{ep['episode_number']:02d} 失败：{exc}", flush=True)
            processed += 1
            continue
        if not seg and not theintrodb_ok:
            stats["error"] += 1  # 只查了 IntroDB、没查 TheIntroDB：不记「没有」，下次重查
            processed += 1
            if retry_none:
                stopped = True
                break
            continue
        db.save_community_segment(ep["id"], provider, "ok" if seg else "none", seg or {})
        stats["found" if seg else "none"] += 1
        processed += 1
        if seg:
            parts = [k[:-6] for k in ("intro_start", "recap_start", "credits_start", "preview_start") if k in seg]
            progress(f"[{i}/{len(episodes)}] {ep['show_title']} S{ep['season_number']:02d}E{ep['episode_number']:02d} {provider}: {'/'.join(parts)}", flush=True)
        if retry_none and not theintrodb_ok:
            stopped = True
            break
    if stopped:
        progress(f"额度用完，停止本轮：已处理 {processed}/{stats['total']} 集", flush=True)
    return stats
