"""审计复现测试：每条发现对应 AUDIT.md 一节，命名 test_FXX_*。

测试断言的是**正确行为**，在当前代码上应失败（失败即复现）。
外部接口一律用假客户端/monkeypatch 注入，不联网。发现清单见 ../../AUDIT.md。
"""

from __future__ import annotations

import pytest

from homecinema import ocr_credits as oc
from homecinema import subtitles as sm
from homecinema.community import CommunityClient, run_community


# ---------- 假 HTTP（community.py 用） ----------

class Resp:
    def __init__(self, code, data=None):
        self.status_code = code
        self._data = data

    def json(self):
        return self._data


class BadJsonResp:
    """HTTP 200 但响应体不是 JSON（网关/CDN 错误页等）。"""

    status_code = 200

    def json(self):
        raise ValueError("Expecting value: line 1 column 1 (char 0)")


class FakeHttp:
    def __init__(self, routes):
        self.routes = routes  # [(url 子串, 参数子集, Resp)]
        self.calls = []

    def get(self, url, params=None, headers=None):
        self.calls.append((url, dict(params or {})))
        for sub, want, resp in self.routes:
            if sub in url and all(params.get(k) == v for k, v in want.items()):
                return resp
        return Resp(404, {"error": "media not found"})


def community_client(routes):
    return CommunityClient(http=FakeHttp(routes), sleep=lambda s: None, min_interval=0)


def _one_episode_show(lib, title, tmdb_id):
    sid = lib.upsert_show(path=f"/tv/{title}", folder=f"/tv/{title}", title=title, year=None)
    lib.update_show_metadata(sid, {"tmdb_id": tmdb_id, "status": "matched"})
    eid = lib.upsert_episode(show_id=sid, season_number=1, episode_number=1,
                             path=f"/tv/{title}/a.mkv", video_name="a", ext=".mkv",
                             title="a", size=1, mtime=0)
    return eid


# ---------- F01 ----------

def test_F01_200_with_bad_json_is_retryable_error_not_permanent_none(lib, monkeypatch):
    """TheIntroDB 返回 200 但响应体不是 JSON：当前被当成「没有数据」写进库，永久跳过；
    正确行为是按可重试错误处理，不落库，下次重跑再查。"""
    eid = _one_episode_show(lib, "Bad JSON Show", 888)
    c = community_client([("theintrodb", {}, BadJsonResp())])
    monkeypatch.setattr("homecinema.community._duration", lambda ep, d: 1500.0)
    stats = run_community(lib, c, tmdb=None, progress=lambda *a, **k: None)
    assert lib.get_community_segment(eid) is None      # 当前代码：已写入 status='none'
    assert stats["error"] == 1 and stats["none"] == 0  # 当前代码：error=0、none=1


# ---------- ocr_credits 帧构造（沿用 tests/test_ocr_credits.py 的约定） ----------

def frame(t, dark=0.95, lines=()):
    return {"t": t, "dark": dark, "lines": [list(x) for x in lines]}


def story(t0, t1):
    sub = (("我为什么要听你的", 0.3, 0.02, 0.4, 0.03),)
    return [frame(t, dark=0.3, lines=sub) for t in range(t0, t1, 2)]


def credits(t0, t1):
    # 每帧名字不同（同一串字出现在三成以上的帧里会被当成台标）
    return [frame(t, lines=((f"Producer Role{chr(65 + t % 26)}", 0.4, 0.55, 0.2, 0.03),
                            (f"PERSON NAME{chr(65 + t % 26)}{chr(65 + t // 26 % 26)}", 0.4, 0.47, 0.2, 0.05)))
            for t in range(t0, t1, 2)]


def episode(frames, duration=1400.0, start=1100.0):
    return {"duration": duration, "start": start, "step": 2.0, "frames": frames}


# ---------- F02 ----------

def test_F02_to_utf8_decodes_big5_correctly():
    """繁体（Big5）字幕：docstring 承诺「GBK/BIG5/UTF-16 都有」都要转成 UTF-8，
    但 gb18030 排在 big5 前且能解开几乎所有 big5 字节 → big5 分支永远走不到，结果是乱码。"""
    text = "片尾字幕：感謝所有工作人員的付出，我們下次再見！\n"
    data = text.encode("big5")
    assert sm.to_utf8(data).decode("utf-8") == text


# ---------- F03 ----------

def test_F03_dark_scene_chinese_dialog_with_fullwidth_punct_not_credits():
    """暗场对白字幕以全角 ？/！ 结尾：_DIALOG 只认半角 ?/!（全角省略号 … 却认），
    中文字幕整行漏判 → 被当成演职员表，与片尾块连成一块，起点提前到对白处。"""
    dialog = [(("有人从帕萨迪纳来，", 0.3, 0.2, 0.4, 0.04), ("加州的，他说", 0.3, 0.16, 0.4, 0.04),
               ("他说他一定会回来？", 0.3, 0.02, 0.4, 0.03)),
              (("你到底在做什么？", 0.3, 0.2, 0.4, 0.04), ("你不是说过吗！", 0.3, 0.16, 0.4, 0.04),
               ("那个人是谁", 0.3, 0.02, 0.4, 0.04))]
    ep = episode(story(1101, 1281)
                 + [frame(t, dark=0.8, lines=dialog[t // 2 % 2]) for t in range(1281, 1301, 2)]
                 + credits(1301, 1399))
    assert oc.decide(ep) == 1300.0


# ---------- F04 ----------

def test_F04_null_item_in_credits_list_does_not_abort_run(lib, monkeypatch):
    """TheIntroDB 200 响应里 credits 列表含 null 项：theintrodb() 抛 AttributeError
    （既不是 RuntimeError 也不是 QuotaExceeded），绕过 run_community 的逐集容错，
    整轮查询命令崩掉。正确行为：null 项当无效条目跳过，有效的 intro 照常落库。"""
    eid = _one_episode_show(lib, "Null Credits Show", 889)
    c = community_client([("theintrodb", {}, Resp(200, {
        "intro": [{"start_ms": 1000, "end_ms": 40000}],
        "credits": [None]}))])
    monkeypatch.setattr("homecinema.community._duration", lambda ep, d: 1500.0)
    stats = run_community(lib, c, tmdb=None, progress=lambda *a, **k: None)
    assert stats["found"] == 1 and stats["error"] == 0
    row = lib.get_community_segment(eid)
    assert row["provider"] == "theintrodb" and row["intro_end"] == 40.0


# ---------- F05 ----------

def test_F05_invalid_config_path_gets_clean_error_not_traceback(capsys):
    """--config 指向带父目录组件的路径是 norm_path **按设计拒绝**的输入类，
    main() 却只接 FileNotFoundError：ValueError 裸奔成 traceback。
    正确行为：与「找不到配置文件」同样干净地报错并返回 2。"""
    from homecinema.__main__ import main
    rc = main(["--config", "../x.toml", "scan"])
    assert rc == 2
    assert "错误" in capsys.readouterr().err
