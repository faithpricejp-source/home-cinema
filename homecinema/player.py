"""播放：交给 IINA（mpv 内核）启动，起后台线程经 mpv JSON IPC 记进度。

关键点：IINA 必须带 --no-stdin 且 stdin 接 DEVNULL，否则后台启动时 IINA 会把
标准输入当片源，打开一个空播放器。启动命令与 IPC 连接都可注入（测试用假 mpv）：
launcher 默认就是 subprocess.Popen，connect 默认是真实 unix socket 连接。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
from pathlib import Path

from .config import Config
from .db import Library

RESUME_MIN_SEC = 30.0        # 位置 < 30 秒当作没看过
WATCHED_RATIO = 0.9          # 位置 >= 时长 90% 判已看
WATCHED_REMAINING_SEC = 180.0  # 或剩余 < 3 分钟判已看

CREDITS_START_MIN_RATIO = 0.6  # 片尾起点要在后 40% 内（>= 时长 60%）才给 credits_start

SUB_LANGS = "chi,zho,zh,chs,zh-Hans,zh-CN,cht,zh-Hant,zh-TW,eng,en"

_CLOSED = object()  # 连接已断开的哨兵


def watched_flag(position: float, duration) -> bool:
    """已看判定（IINA 监控线程与内嵌播放器两条路共用）：
    位置 ≥ 时长 90%；或剩余 < 3 分钟——后者只对 10 分钟以上的片子用，
    否则短片一开播（或刚放几分钟）就被判已看。"""
    if duration and duration > 0 and position > 0:
        near_end = duration > 600 and (duration - position) < WATCHED_REMAINING_SEC
        return position >= WATCHED_RATIO * duration or near_end
    return False


class PlayerError(RuntimeError):
    pass


class Player:
    def __init__(self, db: Library, config: Config, launcher=None, connect=None,
                 poll_interval: float = 5.0, connect_deadline: float = 60.0,
                 startup_grace: float = 120.0):
        self.db = db
        self.config = config
        # 默认 launcher = subprocess.Popen：调用形如
        # Popen([iina_cli, "--no-stdin", file, --mpv-input-ipc-server=..., --mpv-start=...],
        #       stdin=DEVNULL, stdout=DEVNULL, stderr=DEVNULL)
        self._launcher = launcher if launcher is not None else subprocess.Popen
        self._connect = connect if connect is not None else self._default_connect
        self._poll_interval = poll_interval
        self._connect_deadline = connect_deadline
        self._startup_grace = startup_grace
        self._threads: list[threading.Thread] = []

    @staticmethod
    def _default_connect(sock_path: str):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(sock_path)
        return sock

    # ---------- 对外 ----------

    def new_socket_path(self) -> Path:
        d = self.config.sockets_dir
        d.mkdir(parents=True, exist_ok=True)
        return d / f"play-{time.time_ns()}-{os.getpid()}.sock"

    def play(self, item_type: str, item_id: int, sock_path: str | Path | None = None) -> dict:
        path = self._resolve_path(item_type, item_id)
        playback = self.db.get_playback(item_type, item_id)
        start = self._start_position(playback)
        if sock_path is None:
            sock_path = self.new_socket_path()
        else:
            Path(sock_path).parent.mkdir(parents=True, exist_ok=True)
        cmd = [self.config.iina_cli, "--no-stdin", path,
               f"--mpv-input-ipc-server={sock_path}",
               f"--mpv-start={int(start)}",
               # 很多片子内封了字幕但没有一条标成默认，mpv 就不开字幕；按语言优先选中文，其次英文
               f"--mpv-slang={SUB_LANGS}"]
        self._launcher(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
        thread = threading.Thread(target=self._monitor,
                                  args=(str(sock_path), item_type, item_id),
                                  daemon=True, name=f"playback-{item_type}-{item_id}")
        self._threads.append(thread)
        thread.start()
        return {"ok": True, "start_at": start}

    def play_embedded(self, item_type: str, item_id: int) -> dict:
        """内嵌播放：不启动 IINA、不起监控线程，只算出文件路径、续播起点和
        显示标题，返回给 App 外壳，由外壳里的 libmpv 直接播。"""
        path = self._resolve_path(item_type, item_id)
        start = self._start_position(self.db.get_playback(item_type, item_id))
        return {"ok": True, "path": path, "start_at": start,
                "title": self._display_title(item_type, item_id),
                "skip": self.skip_payload(item_type, item_id),
                "subs": self.db.subtitles_for(item_type, item_id)}

    def skip_payload(self, item_type: str, item_id: int) -> dict | None:
        """App 跳过片头片尾用：电影一律 null；剧集给
        {"intro": [start, end] 或 null, "credits_start": 秒 或 null}。"""
        if item_type != "episode":
            return None
        row = self.db.get_segments(item_id)
        if row is None:
            return {"intro": None, "credits_start": None}
        intro = None
        if row["intro_start"] is not None and row["intro_end"] is not None:
            intro = [row["intro_start"], row["intro_end"]]
        return {"intro": intro,
                "credits_start": self._credits_start(item_id, row)}

    def _credits_start(self, episode_id: int, row) -> float | None:
        """片尾起点只在位于该集后 40% 时才给。时长优先用 episodes.runtime_minutes；
        没有就用片尾段自身的 end 近似；再不行（没有起点）不给。"""
        credits_start = row["credits_start"]
        if credits_start is None:
            return None
        episode = self.db.get_episode(episode_id)
        runtime = episode["runtime_minutes"] if episode is not None else None
        if runtime:
            duration = float(runtime) * 60.0
        elif row["credits_end"] is not None:
            duration = float(row["credits_end"])
        else:
            return None
        if credits_start >= duration * CREDITS_START_MIN_RATIO:
            return float(credits_start)
        return None

    def save_progress(self, item_type: str, item_id: int, position: float, duration) -> None:
        """落库一次进度（两条路共用）：内嵌播放器的 /api/progress 与 IINA 监控线程。
        这次没拿到时长时沿用库里已有的，免得把已知时长抹成 NULL、已看被降级。"""
        if not duration:
            prev = self.db.get_playback(item_type, item_id)
            duration = prev["duration_sec"] if prev is not None else None
        self.db.save_playback(item_type, item_id, float(position),
                              float(duration) if duration else None,
                              watched_flag(position, duration))

    def active_count(self) -> int:
        """正在监控进度的播放数（App 退出时据此决定要不要留着服务）。"""
        self._threads = [t for t in self._threads if t.is_alive()]
        return len(self._threads)

    def wait_all(self, timeout: float | None = None) -> None:
        """等全部监控线程退出（测试与优雅退出用）。"""
        for t in list(self._threads):
            t.join(timeout)

    # ---------- 内部 ----------

    def _resolve_path(self, item_type: str, item_id: int) -> str:
        if item_type == "movie":
            row = self.db.get_movie(item_id)
        elif item_type == "episode":
            row = self.db.get_episode(item_id)
        else:
            raise PlayerError("type 必须是 movie 或 episode")
        if row is None:
            raise PlayerError("条目不存在")
        if row["missing"]:
            raise PlayerError("文件已不在原位（missing）")
        return str(row["path"])

    @staticmethod
    def _start_position(playback) -> float:
        if playback is None:
            return 0.0
        if playback["watched"]:
            return 0.0
        pos = playback["position_sec"] or 0.0
        dur = playback["duration_sec"]
        if dur and dur > 0 and pos >= WATCHED_RATIO * dur:
            return 0.0
        if pos < RESUME_MIN_SEC:
            return 0.0
        return float(pos)

    def _monitor(self, sock_path: str, item_type: str, item_id: int) -> None:
        sock = self._wait_for_socket(sock_path)
        if sock is None:
            return  # 等不到 mpv 的 IPC socket，什么都不写
        reader = None
        seen_position = False
        started = time.monotonic()
        try:
            # 对端连上后不回包时 readline 会永远阻塞；超时按断开处理（mpv 正常秒回，取轮询间隔的 4 倍、至少 1 秒）
            sock.settimeout(max(1.0, self._poll_interval * 4))
            reader = sock.makefile("rb")
            while True:
                pos = self._request_property(reader, sock, "time-pos")
                if pos is _CLOSED:
                    break
                dur = self._request_property(reader, sock, "duration")
                if dur is _CLOSED:
                    if pos is not None:  # 刚取到的位置别丢，时长 save_progress 会沿用库里的
                        try:
                            self._save(item_type, item_id, pos, None)
                        except Exception:
                            pass
                    break
                if pos is None and dur is None:
                    # 刚启动时片子还在加载（外置硬盘要先转起来），属性同样拿不到；
                    # 只有播过之后、或加载等太久才算播放器已停止
                    if seen_position or time.monotonic() - started > self._startup_grace:
                        break
                elif pos is not None:
                    seen_position = True
                    try:
                        self._save(item_type, item_id, pos, dur)
                    except Exception:
                        pass  # 一次写库失败（如库被锁）不能让监控线程死掉，下一轮再写
                stop = threading.Event()
                stop.wait(self._poll_interval)
        except OSError:
            pass
        finally:
            if reader is not None:
                try:
                    reader.close()
                except OSError:
                    pass
            try:
                sock.close()
            except OSError:
                pass
        # 每轮轮询都已即时落盘，最后一次位置在断开前已写入

    def _wait_for_socket(self, sock_path: str):
        """mpv 的 IPC socket 要等 IINA 起来才出现；一直连不上就放弃、不写任何进度。"""
        deadline = time.monotonic() + self._connect_deadline
        while True:
            try:
                return self._connect(sock_path)
            except OSError:
                if time.monotonic() >= deadline:
                    return None
                time.sleep(0.25)

    def _request_property(self, reader, sock, name: str):
        """发一条 get_property，读一行 JSON 回包；error=success 取 data。"""
        try:
            line = json.dumps({"command": ["get_property", name]}) + "\n"
            sock.sendall(line.encode("utf-8"))
        except OSError:
            return _CLOSED
        while True:
            try:
                raw = reader.readline()
            except OSError:
                return _CLOSED
            if not raw:
                return _CLOSED
            try:
                obj = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue  # mpv 会主动推事件行，跳过非回包
            if not isinstance(obj, dict) or "error" not in obj:
                continue
            if obj.get("error") != "success":
                return None
            return obj.get("data")

    def _display_title(self, item_type: str, item_id: int) -> str:
        """内嵌播放器的窗口标题：电影是片名；剧集是「剧名 · S01E02 · 单集标题」。"""
        if item_type == "movie":
            return str(self.db.get_movie(item_id)["title"])
        row = self.db.get_episode(item_id)
        return (f"{row['show_title']} · S{row['season_number']:02d}E{row['episode_number']:02d}"
                f" · {row['title']}")

    def _save(self, item_type: str, item_id: int, pos: float, dur) -> None:
        self.save_progress(item_type, item_id, pos, dur)
