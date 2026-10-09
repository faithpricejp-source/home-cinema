"""命令行入口（homecinema/__main__.py）测试：配置加载报错、子命令分发、各命令的
「缺 TMDB key」干净退出，以及成功路径的参数透传与汇总输出。

一律不碰网络：TmdbClient / CommunityClient / 各 run_* 都换成假对象；
serve 只验证 uvicorn.run 收到的参数，绝不真起服务。"""

from __future__ import annotations

from pathlib import Path

import pytest

from homecinema import __main__ as cli


def _write_config(tmp_path, cfg) -> str:
    """把 conftest 的 cfg 字段写成一份真的 config.toml，走 load_config 全流程。"""
    path = tmp_path / "config.toml"
    path.write_text(
        f'movie_roots = ["{cfg.movie_roots[0]}"]\n'
        f'tv_roots = ["{cfg.tv_roots[0]}"]\n'
        f'db_path = "{cfg.db_path}"\n'
        f'cache_dir = "{cfg.cache_dir}"\n'
        f'tmdb_key_file = "{cfg.tmdb_key_file}"\n'
        f'theintrodb_key_file = "{cfg.theintrodb_key_file}"\n'
        f'overrides_file = "{tmp_path / "overrides.toml"}"\n'
        'port = 8799\n',
        encoding="utf-8")
    return str(path)


@pytest.fixture
def use_cfg(cfg, monkeypatch):
    """跳过配置文件，直接把 conftest 的 cfg 交给 main()。"""
    monkeypatch.setattr(cli, "load_config", lambda _path: cfg)
    return cfg


@pytest.fixture
def with_key(cfg):
    path = cfg.tmdb_key_path
    path.write_text("fake-key\n", encoding="utf-8")
    return "fake-key"


class FakeTmdb:
    """记录构造参数的假 TmdbClient；任何网络方法都不该被调用。"""
    instances: list = []

    def __init__(self, key, language):
        self.key, self.language = key, language
        FakeTmdb.instances.append(self)


@pytest.fixture(autouse=True)
def fake_tmdb(monkeypatch):
    FakeTmdb.instances = []
    monkeypatch.setattr("homecinema.metadata.TmdbClient", FakeTmdb)
    return FakeTmdb


# ---------- 配置加载 / 参数解析 ----------

def test_missing_config_file_exits_2(tmp_path, capsys):
    """--config 指向不存在的文件：FileNotFoundError 走干净报错，返回 2。"""
    rc = cli.main(["--config", str(tmp_path / "nope.toml"), "scan"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "错误" in err and "找不到配置文件" in err


@pytest.mark.parametrize("content", [
    'movie_roots = [1, 2]\n',          # 字段类型错：_coerce 抛 ValueError
    'port = = 8770\n',                 # TOML 语法错：TOMLDecodeError 是 ValueError 子类
])
def test_bad_config_content_exits_2(tmp_path, capsys, content):
    path = tmp_path / "config.toml"
    path.write_text(content, encoding="utf-8")
    rc = cli.main(["--config", str(path), "scan"])
    assert rc == 2
    assert "错误" in capsys.readouterr().err


def test_env_config_used_when_no_flag(tmp_path, cfg, monkeypatch, capsys):
    """不给 --config 时读 HOMECINEMA_CONFIG；指向的文件不存在也是干净的 2。"""
    monkeypatch.setenv("HOMECINEMA_CONFIG", str(tmp_path / "env-missing.toml"))
    assert cli.main(["scan"]) == 2
    assert "env-missing.toml" in capsys.readouterr().err


def test_no_subcommand_is_usage_error(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main([])
    assert exc.value.code == 2


# ---------- scan ----------

def test_scan_real_config_empty_library(tmp_path, cfg, capsys):
    """真配置文件 + 空片库目录：完整走 load_config → 分发 → run_scan → 汇总。"""
    (tmp_path / "movies").mkdir()
    (tmp_path / "tv").mkdir()
    rc = cli.main(["--config", _write_config(tmp_path, cfg), "scan"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "扫描完成：电影 0 部、剧集文件 0 集" in out
    assert cfg.db_file.exists()


def test_scan_closes_db_even_when_scan_raises(use_cfg, monkeypatch):
    """run_scan 抛异常时异常照常外抛，但库连接要在 finally 里关掉。"""
    closed = []
    orig_close = cli.Library.close

    def tracking_close(self):
        closed.append(True)
        orig_close(self)

    monkeypatch.setattr(cli.Library, "close", tracking_close)

    def boom(cfg, db):
        raise RuntimeError("扫描炸了")

    monkeypatch.setattr("homecinema.scanner.run_scan", boom)
    with pytest.raises(RuntimeError):
        cli.main(["scan"])
    assert closed == [True]


# ---------- 缺 TMDB key 的命令统一返回 2 ----------

@pytest.mark.parametrize("command", ["fetch-metadata", "recommend", "fetch-extras", "fetch-titles"])
def test_tmdb_commands_without_key_exit_2(use_cfg, capsys, command):
    """key 文件不存在：不建客户端、不开库，报出 key 路径并返回 2。"""
    rc = cli.main([command])
    assert rc == 2
    err = capsys.readouterr().err
    assert "TMDB key" in err and str(use_cfg.tmdb_key_path) in err
    assert FakeTmdb.instances == []


def test_empty_key_file_counts_as_missing(use_cfg, capsys):
    use_cfg.tmdb_key_path.write_text("  \n", encoding="utf-8")
    assert cli.main(["fetch-metadata"]) == 2
    assert "TMDB key" in capsys.readouterr().err


# ---------- fetch-metadata ----------

def test_fetch_metadata_passes_refresh_and_progress(use_cfg, with_key, monkeypatch, capsys):
    seen = {}

    class FakeService:
        def __init__(self, db, client, images_dir, progress, refresh, overrides):
            seen.update(client=client, images_dir=images_dir, refresh=refresh,
                        overrides=overrides)
            self.progress = progress

        def enrich_all(self):
            self.progress(1, 2, "虚构电影甲")
            return {"matched": 1, "unmatched": 1, "total": 2}  # 没有 failed 键也要能打印

    monkeypatch.setattr("homecinema.metadata.MetadataService", FakeService)
    rc = cli.main(["fetch-metadata", "--refresh"])
    assert rc == 0
    assert seen["refresh"] is True
    assert seen["client"].key == with_key and seen["client"].language == "zh-CN"
    assert seen["images_dir"] == use_cfg.images_dir
    assert seen["overrides"] == {"movie": {}, "tv": {}}
    out = capsys.readouterr().out
    assert "[1/2] 虚构电影甲" in out
    assert "匹配 1，未匹配 1，出错 0（共 2）" in out


# ---------- detect-segments ----------

def test_detect_segments_forwards_options(use_cfg, monkeypatch, capsys):
    seen = {}

    def fake_detect(db, show_id=None, limit_seasons=None):
        seen.update(show_id=show_id, limit_seasons=limit_seasons)
        return 3

    monkeypatch.setattr("homecinema.detect.run_detect_segments", fake_detect)
    rc = cli.main(["detect-segments", "--show", "7", "--limit-seasons", "2"])
    assert rc == 0
    assert seen == {"show_id": 7, "limit_seasons": 2}
    assert "处理 3 季" in capsys.readouterr().out


# ---------- recommend ----------

def test_recommend_forwards_flags(use_cfg, with_key, monkeypatch, capsys):
    seen = {}

    def fake_generate(db, client, cache_dir, images_dir, refresh, progress, rerank):
        seen.update(cache_dir=cache_dir, refresh=refresh, rerank=rerank)
        progress(1, 1, "虚构剧乙")
        return {"movies": 4, "shows": 5, "sources": 6}

    monkeypatch.setattr("homecinema.recommend.generate", fake_generate)
    rc = cli.main(["recommend", "--rerank"])
    assert rc == 0
    assert seen == {"cache_dir": use_cfg.cache_dir_path, "refresh": False, "rerank": True}
    out = capsys.readouterr().out
    assert "[1/1] 虚构剧乙" in out and "电影 4 部、剧集 5 部（来源 6 条）" in out


# ---------- community-segments ----------

class FakeCommunity:
    def __init__(self, theintrodb_key=None):
        self.theintrodb_key = theintrodb_key


@pytest.fixture
def fake_run_community(monkeypatch):
    seen = {}

    def fake(db, client, tmdb=None, ocr_cache_dir="", refresh=False,
             retry_none=False, limit=None):
        seen.update(client=client, tmdb=tmdb, ocr_cache_dir=ocr_cache_dir,
                    refresh=refresh, retry_none=retry_none, limit=limit)
        return {"total": 3, "found": 1, "none": 1, "error": 1}

    monkeypatch.setattr("homecinema.community.CommunityClient", FakeCommunity)
    monkeypatch.setattr("homecinema.community.run_community", fake)
    return seen


def test_community_without_tmdb_key_still_runs(use_cfg, fake_run_community, capsys):
    """社区库不强制 TMDB key：没 key 时 tmdb=None 照跑，TheIntroDB key 透传。"""
    Path(use_cfg.theintrodb_key_file).write_text("tidb-fake\n", encoding="utf-8")
    rc = cli.main(["community-segments", "--refresh"])
    assert rc == 0
    seen = fake_run_community
    assert seen["tmdb"] is None and FakeTmdb.instances == []
    assert seen["client"].theintrodb_key == "tidb-fake"
    assert seen["refresh"] is True and seen["retry_none"] is False
    assert seen["ocr_cache_dir"].endswith("ocr_tail")
    out = capsys.readouterr().out
    assert "社区片头片尾库查询完成：共 3 集" in out


def test_community_retry_none_with_limit(use_cfg, with_key, fake_run_community, capsys):
    rc = cli.main(["community-segments", "--retry-none", "--limit", "5"])
    assert rc == 0
    seen = fake_run_community
    assert seen["retry_none"] is True and seen["limit"] == 5
    assert isinstance(seen["tmdb"], FakeTmdb) and seen["tmdb"].key == with_key
    assert "重查 none 完成：重查 3 集" in capsys.readouterr().out


# ---------- fetch-extras / fetch-titles ----------

def test_fetch_extras_runs_three_steps(use_cfg, with_key, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr("homecinema.extras.run_extras",
                        lambda db, client, images_dir, refresh: calls.append(("extras", refresh))
                        or {"ok": 2, "failed": 1, "total": 3})
    monkeypatch.setattr("homecinema.extras.run_people_aka",
                        lambda db, client, refresh: calls.append(("aka", refresh))
                        or {"ok": 4, "failed": 0})
    monkeypatch.setattr("homecinema.extras.probe_heights",
                        lambda db: calls.append(("heights",)) or 7)
    rc = cli.main(["fetch-extras", "--refresh"])
    assert rc == 0
    assert calls == [("extras", True), ("aka", True), ("heights",)]
    out = capsys.readouterr().out
    assert "2 部成功、1 部失败（共 3）" in out and "人名别名 4 人成功" in out and "补分辨率 7 部" in out


def test_fetch_titles(use_cfg, with_key, monkeypatch, capsys):
    seen = {}

    def fake_fill(db, client, refresh=False, progress=None):
        seen.update(client=client, refresh=refresh)
        progress(1, 1, "虚构电影丙")
        return {"ok": 1, "failed": 0, "total": 1}

    monkeypatch.setattr("homecinema.metadata.fill_aka", fake_fill)
    rc = cli.main(["fetch-titles"])
    assert rc == 0
    assert seen["refresh"] is False and seen["client"].key == with_key
    out = capsys.readouterr().out
    assert "[1/1] 虚构电影丙" in out and "中文译名完成：1 部成功" in out


# ---------- serve ----------

def test_serve_binds_localhost_with_config_port(use_cfg, monkeypatch):
    """serve 只监听 127.0.0.1，端口取配置；uvicorn.run 换成假函数，不真起服务。"""
    import uvicorn
    sentinel = object()
    seen = {}
    monkeypatch.setattr("homecinema.server.create_app",
                        lambda config: seen.setdefault("config", config) and sentinel)
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: seen.update(app=app, **kw))
    assert cli.main(["serve"]) == 0
    assert seen["config"] is use_cfg and seen["app"] is sentinel
    assert seen["host"] == "127.0.0.1" and seen["port"] == use_cfg.port
