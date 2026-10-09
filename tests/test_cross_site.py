"""跨站请求防护：写方法只收本机/Tailscale 主机、application/json、同源 Origin。

「放行」用 POST /api/watched 打一个不存在的条目：拿到路由自己的 404（「条目不存在」）
就说明请求过了防护、进了端点；被防护拦下是 403。
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from homecinema.player import Player
from homecinema.server import CrossSiteGuard, create_app

BODY = {"type": "movie", "id": 9999, "watched": True}


@pytest.fixture
def app(cfg, lib):
    def launcher(cmd, stdin=None, stdout=None, stderr=None):
        raise AssertionError("测试里不许真启动播放器")

    return create_app(config=cfg, db=lib, player=Player(lib, cfg, launcher=launcher))


def client(app, base="http://127.0.0.1:8770"):
    return TestClient(app, base_url=base)


def reached_endpoint(resp) -> bool:
    return resp.status_code == 404 and resp.json()["detail"] == "条目不存在"


def test_same_origin_json_allowed(app):
    r = client(app).post("/api/watched", json=BODY, headers={"Origin": "http://127.0.0.1:8770"})
    assert reached_endpoint(r)


def test_text_plain_simple_request_refused(app):
    r = client(app).post("/api/watched", content=b'{"type": "movie", "id": 9999, "watched": true}',
                         headers={"Content-Type": "text/plain"})
    assert r.status_code == 403 and "content-type" in r.json()["detail"]


def test_bodiless_post_without_content_type_refused(app):
    # /api/scan 这类无请求体端点，跨站表单/简单请求最容易打中
    assert client(app).post("/api/scan").status_code == 403


def test_foreign_origin_refused(app):
    r = client(app).post("/api/watched", json=BODY, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403


def test_rebound_host_refused(app):
    r = client(app, base="http://evil.example:8770").post("/api/watched", json=BODY)
    assert r.status_code == 403 and "host" in r.json()["detail"]


def test_tailnet_host_allowed(app):
    r = client(app, base="https://macbox.tailabc.ts.net").post(
        "/api/watched", json=BODY, headers={"Origin": "https://macbox.tailabc.ts.net",
                                            "Content-Type": "application/json; charset=utf-8"})
    assert reached_endpoint(r)


def test_non_object_json_gets_error_response_not_dropped(app):
    r = client(app).post("/api/watched", content=b"[1]", headers={"Content-Type": "application/json"})
    assert r.status_code == 422 and "detail" in r.json()


def test_get_not_affected(app):
    assert client(app, base="http://evil.example").get("/api/home").status_code == 200


@pytest.mark.parametrize("length", ["-1", "abc", str(50 * 1024 * 1024 + 1)])
def test_bad_content_length_refused(length):
    """直接驱动中间件：httpx 会自己算 Content-Length，伪造不了负数/超大值。"""
    async def inner(scope, receive, send):
        raise AssertionError("不该放行到应用")

    sent = []

    async def send(msg):
        sent.append(msg)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {"type": "http", "method": "POST", "path": "/api/watched", "raw_path": b"/api/watched",
             "query_string": b"", "headers": [(b"host", b"127.0.0.1:8770"),
                                               (b"content-type", b"application/json"),
                                               (b"content-length", length.encode())]}
    asyncio.run(CrossSiteGuard(inner)(scope, receive, send))
    assert sent[0]["status"] == 400
