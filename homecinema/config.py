"""配置加载：config.toml（路径可用 HOMECINEMA_CONFIG 覆盖）。"""

from __future__ import annotations

import os
import tomllib
import unicodedata
from dataclasses import dataclass, field, fields
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent


def norm_path(value: str | Path) -> Path:
    """展开 ~ 并规范化；拒绝指向目录树上方（父目录组件）的相对路径，
    引用上级目录请写绝对路径。"""
    p = Path(value).expanduser()
    for part in p.parts:
        if part == ".." or (len(part) > 1 and set(part) == {"."}):
            raise ValueError(f"路径不允许包含父目录组件：{value!r}")
    return p.resolve()


@dataclass
class Config:
    movie_roots: list[str] = field(default_factory=list)
    tv_roots: list[str] = field(default_factory=list)
    db_path: str = "~/Library/Application Support/HomeCinema/library.db"
    cache_dir: str = "~/Library/Caches/HomeCinema"
    tmdb_key_file: str = "~/.config/tmdb/api-key.txt"
    tmdb_language: str = "zh-CN"
    port: int = 8770
    iina_cli: str = "/Applications/IINA.app/Contents/MacOS/iina-cli"
    # 手动指定匹配：[movie] / [tv] 两节，键是文件夹名，值是 TMDB 编号
    overrides_file: str = "~/Library/Application Support/HomeCinema/overrides.toml"

    # 展开过 ~ 的路径
    @property
    def db_file(self) -> Path:
        return norm_path(self.db_path)

    @property
    def cache_dir_path(self) -> Path:
        return norm_path(self.cache_dir)

    @property
    def images_dir(self) -> Path:
        return self.cache_dir_path / "images"

    @property
    def sockets_dir(self) -> Path:
        return self.cache_dir_path / "sockets"

    @property
    def tmdb_key_path(self) -> Path:
        return norm_path(self.tmdb_key_file)

    def read_overrides(self) -> dict:
        """{"movie": {文件夹名: tmdb_id}, "tv": {...}}；文件不存在就是空。"""
        try:
            data = tomllib.loads(norm_path(self.overrides_file).read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            return {"movie": {}, "tv": {}}
        # 文件夹名统一成 NFC：macOS 上 "ä"、"ō" 可能以分解形式存储，跟手写的键对不上
        return {k: {unicodedata.normalize("NFC", str(n)): int(v)
                    for n, v in (data.get(k) or {}).items()} for k in ("movie", "tv")}

    def read_tmdb_key(self) -> str | None:
        """只从 key 文件读，不进代码、不进日志。"""
        p = self.tmdb_key_path
        try:
            text = p.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return text or None


def _coerce(cfg: "Config", key: str, value) -> None:
    if key in ("movie_roots", "tv_roots"):
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ValueError(f"config 字段 {key} 必须是字符串列表")
    setattr(cfg, key, value)


def load_config(path: str | Path | None = None) -> Config:
    """读 config.toml；默认 <项目根>/config.toml，可用环境变量 HOMECINEMA_CONFIG 覆盖。"""
    if path is None:
        path = os.environ.get("HOMECINEMA_CONFIG") or (PACKAGE_ROOT / "config.toml")
    # norm_path：展开 ~、拒绝父目录组件、resolve 成绝对路径后再读取
    cfg_path = norm_path(path)
    if not cfg_path.is_file():
        raise FileNotFoundError(
            f"找不到配置文件 {cfg_path}；请先复制 config.example.toml 为 config.toml 并按需修改"
        )
    data = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
    cfg = Config()
    known = {f.name for f in fields(cfg)}
    for key, value in data.items():
        if key in known:
            _coerce(cfg, key, value)
    return cfg
