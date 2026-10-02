"""配置加载测试。"""

from __future__ import annotations

import pytest

from homecinema.config import Config, load_config, norm_path


def test_load_config_reads_toml(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        'movie_roots = ["/tmp/films"]\n'
        'tv_roots = ["/tmp/series"]\n'
        'port = 9000\n'
        'tmdb_language = "en-US"\n',
        encoding="utf-8")
    cfg = load_config(path)
    assert cfg.movie_roots == ["/tmp/films"]
    assert cfg.tv_roots == ["/tmp/series"]
    assert cfg.port == 9000
    assert cfg.tmdb_language == "en-US"
    # 未写的字段用默认值
    assert cfg.iina_cli == "/Applications/IINA.app/Contents/MacOS/iina-cli"


def test_load_config_env_override(tmp_path, monkeypatch):
    path = tmp_path / "elsewhere.toml"
    path.write_text('port = 9100\n', encoding="utf-8")
    monkeypatch.setenv("HOMECINEMA_CONFIG", str(path))
    assert load_config().port == 9100
    assert load_config(str(path)).port == 9100


def test_load_config_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "nope.toml")


def test_norm_path_rejects_parent_components(tmp_path):
    assert norm_path(str(tmp_path / "x")).is_absolute()
    with pytest.raises(ValueError):
        norm_path("../up.toml")
    with pytest.raises(ValueError):
        norm_path(f"{tmp_path}/a/../b")


def test_read_tmdb_key(tmp_path):
    cfg = Config(tmdb_key_file=str(tmp_path / "key.txt"))
    assert cfg.read_tmdb_key() is None  # 文件不存在
    (tmp_path / "key.txt").write_text("  secret-key \n", encoding="utf-8")
    assert cfg.read_tmdb_key() == "secret-key"
