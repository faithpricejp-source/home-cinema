"""中文字幕：射手网假客户端 + 虚构片名。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from homecinema import subtitles as sm
from homecinema.player import Player
from homecinema.scanner import run_scan
from homecinema.server import ScanService, create_app


class Resp:
    def __init__(self, code, data=None, content=b""):
        self.status_code = code
        self._data = data
        self.content = content

    def json(self):
        if self._data is None:
            raise ValueError
        return self._data


class FakeHttp:
    def __init__(self):
        self.calls = []

    def get(self, url, params=None, headers=None):
        self.calls.append(url)
        if url.endswith("/sub/search"):
            q = params["q"]
            if "no_muxer" in params:  # 按文件名搜不到，退回片名
                return Resp(200, {"status": 0, "sub": {"subs": []}})
            return Resp(200, {"status": 0, "sub": {"subs": [
                {"id": 11, "native_name": "示例电影", "lang": {"desc": "英", "langlist": {"langeng": True}}},
                {"id": 12, "native_name": "示例电影 " + q, "subtype": "Subrip(srt)",
                 "lang": {"desc": "简英", "langlist": {"langchs": True, "langeng": True}}}]}})
        if url.endswith("/sub/detail"):
            return Resp(200, {"status": 0, "sub": {"subs": [{"id": 12, "filelist": [
                {"f": "Example.cht.srt", "url": "https://file/cht"},
                {"f": "Example.chs.ass", "url": "https://file/chs"},
                {"f": "Example.sup", "url": "https://file/sup"}]}]}})
        if url == "https://file/chs":
            return Resp(200, content=b"[Script Info]\n")
        return Resp(404, {"status": 20900})


def test_search_keeps_chinese_and_falls_back_to_title():
    c = sm.AssrtClient("t", http=FakeHttp(), sleep=lambda s: None, min_interval=0)
    res = sm.search_candidates(c, "Example.Movie.2001.1080p.BluRay-GRP", "Example Movie", 2001)
    assert [r["id"] for r in res] == [12]
    assert res[0]["lang"] == "简英"


def test_download_prefers_simplified_text_sub(tmp_path):
    c = sm.AssrtClient("t", http=FakeHttp(), sleep=lambda s: None, min_interval=0)
    path = sm.download_best_file(c, 12, tmp_path, "Ex:ample")
    assert path.name == "Ex_ample.12.zh.ass"  # ':' 会破坏 mpv 的路径列表，换掉
    assert path.read_bytes() == b"[Script Info]\n"


def test_has_zh_word_boundaries():
    assert sm._has_zh("chi") and sm._has_zh("Movie.zh.srt") and sm._has_zh("简体中文")
    assert not sm._has_zh("zhang yimou interview") and not sm._has_zh("english")


@pytest.fixture
def client(tmp_path, cfg, lib, monkeypatch):
    folder = tmp_path / "movies" / "Example Movie (2001)"
    folder.mkdir(parents=True)
    (folder / "Example Movie (2001).mp4").write_bytes(b"")
    run_scan(cfg, lib)
    monkeypatch.setattr(sm, "embedded_status", lambda path, whole_folder=False:
                        {"embedded": ["eng"], "external": [], "has_zh": False})
    app = create_app(config=cfg, db=lib, player=Player(lib, cfg, launcher=lambda *a, **k: None),
                     scan_service=ScanService(cfg, lib))
    with TestClient(app) as tc:
        yield tc, lib, cfg, lib.list_movies()[0]["id"]


def test_subtitle_api_flow(client, monkeypatch):
    tc, lib, cfg, mid = client
    st = tc.get(f"/api/subtitles?type=movie&id={mid}").json()
    assert st["has_zh"] is False and st["search_enabled"] is False
    assert tc.get(f"/api/subtitles/search?type=movie&id={mid}").status_code == 409  # 没 token
    token = Path(cfg.assrt_token_file)
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text("tok")
    http = FakeHttp()
    orig = sm.AssrtClient
    monkeypatch.setattr(sm, "AssrtClient", lambda t: orig(t, http=http, sleep=lambda s: None, min_interval=0))
    assert [r["id"] for r in tc.get(f"/api/subtitles/search?type=movie&id={mid}").json()["results"]] == [12]
    d = tc.post("/api/subtitles/download", json={"type": "movie", "id": mid, "sub_id": 12}).json()
    assert d["name"].endswith(".12.zh.ass")
    st = tc.get(f"/api/subtitles?type=movie&id={mid}").json()
    assert st["has_zh"] is True and st["downloaded"] == [d["name"]]
    play = tc.post("/api/play", json={"type": "movie", "id": mid, "embedded": True}).json()
    assert len(play["subs"]) == 1 and play["subs"][0].endswith(d["name"])
    assert tc.post("/api/subtitles/remove", json={"type": "movie", "id": mid, "name": d["name"]}).json() == {"ok": True}
    assert tc.post("/api/play", json={"type": "movie", "id": mid, "embedded": True}).json()["subs"] == []
    assert tc.get("/api/subtitles?type=show&id=1").status_code == 422


def test_to_utf8_converts_gbk_utf16_and_keeps_utf8():
    s = "1\n00:00:01,000 --> 00:00:02,000\n天哪，他们要在这里做？\n"
    assert sm.to_utf8(s.encode("gb18030")).decode("utf-8") == s
    assert sm.to_utf8(s.encode("utf-16")).decode("utf-8") == s
    assert sm.to_utf8(("\ufeff" + s).encode("utf-8")).decode("utf-8") == s
