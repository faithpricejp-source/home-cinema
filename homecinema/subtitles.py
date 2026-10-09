"""中文字幕：查片子自带的字幕（内封 + 同目录外挂），没有中文时从射手网（assrt.net）搜索下载。

下载的字幕放 ~/Library/Application Support/HomeCinema/subs/，不写进影片目录；
播放时经 /api/play 的 subs 字段交给播放器加载。射手网要求注明来源：「字幕服务由 assrt.net 提供」。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

from .segments import _tool  # Kimi-E-5：ffprobe 解析复用 segments 的 PATH+Homebrew 回退

ASSRT = "https://api.assrt.net/v1"
SUB_EXTS = (".srt", ".ass", ".ssa", ".vtt", ".sub", ".idx", ".sup")
TEXT_SUB_EXTS = (".srt", ".ass", ".ssa", ".vtt")  # 只下载文本字幕（libmpv 都能直接加载）
_ZH_HINTS = ("chi", "zho", "chs", "cht", "zh", "简", "繁", "中")
MIN_INTERVAL = 3.1  # 射手网配额 20 次/分钟（每 token、每 IP 共享）


def _has_zh(text: str) -> bool:
    t = (text or "").lower()
    # zh/chs/cht 要按词边界判，免得 "zhang"、"which" 误判
    return any(re.search(r"(?<![a-z])" + h + r"(?![a-z])", t) if h.isascii() else h in t for h in _ZH_HINTS)


def embedded_status(path: str, whole_folder: bool = False) -> dict:
    """片子自带的字幕：{"embedded": [描述...], "external": [文件名...], "has_zh": bool}。
    whole_folder：电影一部一个文件夹，文件夹里的字幕都算；剧集一季一个文件夹，只认同名前缀的。"""
    embedded = []
    try:
        out = subprocess.run([_tool("ffprobe"), "-v", "error", "-select_streams", "s",
                              "-show_entries", "stream=codec_name:stream_tags=language,title",
                              "-of", "json", path], capture_output=True, text=True, timeout=60).stdout
        for s in json.loads(out or "{}").get("streams", []):
            tags = s.get("tags") or {}
            embedded.append(" ".join(x for x in (tags.get("language"), tags.get("title")) if x) or s.get("codec_name", "?"))
    except (subprocess.SubprocessError, OSError, ValueError):
        pass
    external = []
    folder = os.path.dirname(path)
    # Kimi-E-4：前缀匹配也统一小写——扩展名已 lower()，唯独 stem 没有，
    # 下载器常见的全小写字幕名（show.s01e01.…）会漏判
    stem = os.path.splitext(os.path.basename(path))[0].lower()
    try:
        for name in os.listdir(folder):
            if name.lower().endswith(SUB_EXTS) and (whole_folder or name.lower().startswith(stem)):
                external.append(name)
    except OSError:
        pass
    has_zh = any(_has_zh(e) for e in embedded) or any(_has_zh(x) for x in external)
    return {"embedded": embedded, "external": external, "has_zh": has_zh}


class AssrtClient:
    def __init__(self, token: str, http=None, sleep=time.sleep, clock=time.monotonic,
                 min_interval: float = MIN_INTERVAL):
        self.token = token
        self._http = http
        self._sleep = sleep
        self._clock = clock
        self._min_interval = min_interval
        self._last = clock() - min_interval

    def _client(self):
        if self._http is None:
            import httpx
            self._http = httpx.Client(timeout=20.0, follow_redirects=True)
        return self._http

    def _throttle(self):
        wait = self._min_interval - (self._clock() - self._last)
        if wait > 0:
            self._sleep(wait)
        self._last = self._clock()

    def _api(self, endpoint: str, params: dict) -> dict:
        self._throttle()
        resp = self._client().get(ASSRT + endpoint, params=params,
                                  headers={"Authorization": "Bearer " + self.token})
        try:
            data = resp.json()
        except ValueError:
            raise RuntimeError(f"射手网返回非 JSON（HTTP {resp.status_code}）")
        if data.get("status") not in (0, None):
            raise RuntimeError(f"射手网错误 {data.get('status')}：{data.get('errmsg') or data.get('error') or ''}".strip("："))
        return data

    def search(self, query: str, is_file: bool = False) -> list[dict]:
        params = {"q": query[:120], "cnt": 15}
        if is_file:
            params["no_muxer"] = 1
        data = self._api("/sub/search", params)
        subs = (data.get("sub") or {}).get("subs") or []
        return [s for s in subs if isinstance(s, dict)]

    def detail(self, sub_id: int) -> dict | None:
        data = self._api("/sub/detail", {"id": int(sub_id)})
        subs = (data.get("sub") or {}).get("subs") or []
        return subs[0] if subs else None

    def download(self, url: str) -> bytes:
        self._throttle()
        resp = self._client().get(url)
        if resp.status_code != 200 or not resp.content:
            raise RuntimeError(f"字幕下载失败（HTTP {resp.status_code}）")
        return resp.content


def candidate_payload(s: dict) -> dict:
    lang = s.get("lang") or {}
    return {"id": s.get("id"), "name": s.get("native_name") or s.get("videoname") or "",
            "videoname": s.get("videoname") or "", "lang": lang.get("desc") or "",
            "subtype": s.get("subtype") or "", "site": s.get("release_site") or "",
            "score": s.get("vote_score") or 0, "uploaded": s.get("upload_time") or ""}


def search_candidates(client: AssrtClient, video_name: str, title: str, year) -> list[dict]:
    """先按文件名搜（去掉压制组/参数），没结果再按片名+年份。只留含中文的。"""
    seen, out = set(), []
    queries = [(video_name, True)]
    if title:
        queries.append((f"{title} {year}" if year else title, False))
    for q, is_file in queries:
        if len(q.strip()) < 3:
            continue
        for s in client.search(q, is_file=is_file):
            desc = ((s.get("lang") or {}).get("desc") or "")
            langs = (s.get("lang") or {}).get("langlist") or {}
            zh = any(k in langs for k in ("langchs", "langcht", "langdou")) or any(c in desc for c in "简繁双中")
            if s.get("id") in seen or not zh:
                continue
            seen.add(s.get("id"))
            out.append(candidate_payload(s))
        if out:
            break
    return out


def to_utf8(data: bytes) -> bytes:
    """字幕统一转 UTF-8：射手网上 GBK/BIG5/UTF-16 都有，不靠播放器猜编码。认不出原样返回。"""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        candidates = ("utf-16",)
    else:
        candidates = ("utf-8-sig", "gb18030", "big5")
    for enc in candidates:
        try:
            text = data.decode(enc)
        except UnicodeDecodeError:
            continue
        # 10-05 审计 F02：Big5 字节几乎都能被 gb18030 解开但成乱码（大量私用区字符），换 big5 再试
        if enc == "gb18030" and _pua_ratio(text) > 0.01:
            try:
                return data.decode("big5").encode("utf-8")
            except UnicodeDecodeError:
                pass
        return text.encode("utf-8")
    return data


def _pua_ratio(text: str) -> float:
    if not text:
        return 0.0
    return sum(1 for ch in text if "\ue000" <= ch <= "\uf8ff") / len(text)


def _safe_name(name: str) -> str:
    # 去掉路径分隔和 mpv 路径列表分隔符 ':'
    return re.sub(r"[/:\\\x00]", "_", name).strip() or "subtitle.srt"


def download_best_file(client: AssrtClient, sub_id: int, dest_dir: Path, label: str) -> Path:
    """取字幕包里最合适的一个文本字幕文件：优先简体/双语 srt/ass，存成 <label>.<sub_id>.zh.<ext>。"""
    info = client.detail(sub_id)
    if not info:
        raise RuntimeError("字幕不存在")
    files = [f for f in (info.get("filelist") or []) if f.get("url") and f.get("f", "").lower().endswith(TEXT_SUB_EXTS)]
    if not files and (info.get("filename") or "").lower().endswith(TEXT_SUB_EXTS) and info.get("url"):
        files = [{"f": info["filename"], "url": info["url"]}]
    if not files:
        raise RuntimeError("这个字幕包里没有 srt/ass 文本字幕（可能是图形字幕或压缩包），换一个试试")

    def rank(f):
        n = f["f"].lower()
        return (0 if any(k in n for k in ("chs", "简", "sc.", ".zh", "gb")) else
                1 if any(k in n for k in ("双语", "chs&eng", "chs.eng", "&")) else
                2 if any(k in n for k in ("cht", "繁", "big5", "tc.")) else 3,
                0 if n.endswith(".ass") else 1)

    best = sorted(files, key=rank)[0]
    data = to_utf8(client.download(best["url"]))
    ext = os.path.splitext(best["f"])[1].lower()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / _safe_name(f"{label}.{sub_id}.zh{ext}")
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, dest)
    return dest
