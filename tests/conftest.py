"""测试公共 fixture：临时配置、库、目录树建造器。一律用虚构片名。"""

from __future__ import annotations

import shutil
import tempfile
import time
from pathlib import Path

import pytest

from homecinema.config import Config
from homecinema.db import Library


@pytest.fixture
def cfg(tmp_path) -> Config:
    return Config(
        movie_roots=[str(tmp_path / "movies")],
        tv_roots=[str(tmp_path / "tv")],
        db_path=str(tmp_path / "lib.db"),
        cache_dir=str(tmp_path / "cache"),
        tmdb_key_file=str(tmp_path / "tmdb-key.txt"),
        assrt_token_file=str(tmp_path / "assrt-token.txt"),
        theintrodb_key_file=str(tmp_path / "tidb-key.txt"),
        tmdb_language="zh-CN",
        port=8770,
        iina_cli="/fake/iina-cli",
    )


@pytest.fixture
def lib(cfg) -> Library:
    db = Library(cfg.db_file)
    yield db
    db.close()


@pytest.fixture
def sock_dir():
    """unix socket 路径有 104 字符上限，pytest 的 tmp_path 太深，用短路径。"""
    d = tempfile.mkdtemp(prefix="hc-sock-")
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


def wait_until(fn, timeout: float = 5.0, interval: float = 0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(interval)
    return fn()

