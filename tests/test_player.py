"""播放测试：假 mpv unix socket 服务端；启动命令可注入。"""

from __future__ import annotations

import json
import socket
import subprocess
import threading

from conftest import wait_until

from homecinema.player import Player
from homecinema.scanner import run_scan


class FakeMpvServer:
    """模拟 mpv JSON IPC：按脚本返回 time-pos 序列；序列耗尽即断开（模拟退出）。"""

    def __init__(self, sock_path, time_pos_seq, duration=100.0):
        self.sock_path = str(sock_path)
        self.time_pos_seq = list(time_pos_seq)
        self.duration = duration
        self.commands = []
        self._running = True
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(self.sock_path)
        self._srv.listen(4)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while self._running:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                return
            self._handle(conn)

    def _handle(self, conn):
        reader = conn.makefile("rb")
        try:
            while True:
                line = reader.readline()
                if not line:
                    break
                cmd = json.loads(line.decode("utf-8"))
                self.commands.append(cmd)
                name = cmd["command"][1]
                if name == "time-pos":
                    if not self.time_pos_seq:
                        break  # 断开连接 → 播放器退出
                    value = self.time_pos_seq.pop(0)
                    conn.sendall(json.dumps({"error": "success", "data": value}).encode() + b"\n")
                elif name == "duration":
                    conn.sendall(json.dumps({"error": "success", "data": self.duration}).encode() + b"\n")
                else:
                    conn.sendall(json.dumps({"error": "success", "data": None}).encode() + b"\n")
        finally:
            reader.close()
            conn.close()

    def close(self):
        self._running = False
        try:
            self._srv.close()
        except OSError:
            pass


def make_player(cfg, lib, captured):
    def launcher(cmd, stdin=None, stdout=None, stderr=None):
        captured["cmd"] = list(cmd)
        captured["stdin"] = stdin
        captured["stdout"] = stdout
        captured["stderr"] = stderr

    return Player(lib, cfg, launcher=launcher, poll_interval=0.05, connect_deadline=3.0)


def seed_movie(tmp_path, cfg, lib):
    movies = tmp_path / "movies"
    folder = movies / "Example Movie (2001)"
    folder.mkdir(parents=True)
    video = folder / "Example Movie (2001).mp4"
    video.write_bytes(b"")
    run_scan(cfg, lib)
    return lib.list_movies()[0]["id"]


def seed_playback(lib, movie_id, position, duration, watched):
    lib.save_playback("movie", movie_id, position, duration, watched)


def test_play_launches_iina_with_no_stdin_and_devnull(tmp_path, cfg, lib, sock_dir):
    movie_id = seed_movie(tmp_path, cfg, lib)
    captured = {}
    player = make_player(cfg, lib, captured)
    sock = sock_dir / "play.sock"
    server = FakeMpvServer(sock, time_pos_seq=[5], duration=3600)
    try:
        result = player.play("movie", movie_id, sock_path=sock)
        assert result == {"ok": True, "start_at": 0.0}
        player.wait_all(timeout=5)
    finally:
        server.close()

    cmd = captured["cmd"]
    assert cmd[0] == "/fake/iina-cli"
    assert cmd[1] == "--no-stdin"  # 必须带 --no-stdin
    assert cmd[2].endswith("Example Movie (2001).mp4")
    assert any(c.startswith("--mpv-input-ipc-server=") for c in cmd)
    assert "--mpv-start=0" in cmd
    assert captured["stdin"] == subprocess.DEVNULL  # stdin 接 DEVNULL
    assert captured["stdout"] == subprocess.DEVNULL
    assert captured["stderr"] == subprocess.DEVNULL


def test_progress_persisted_each_poll(tmp_path, cfg, lib, sock_dir):
    movie_id = seed_movie(tmp_path, cfg, lib)
    captured = {}
    player = make_player(cfg, lib, captured)
    sock = sock_dir / "play.sock"
    server = FakeMpvServer(sock, time_pos_seq=[5, 12, 31], duration=3600)
    try:
        player.play("movie", movie_id, sock_path=sock)

        def reached():
            pb = lib.get_playback("movie", movie_id)
            return pb if pb is not None and pb["position_sec"] == 31 else None

        wait_until(reached)
        player.wait_all(timeout=5)
    finally:
        server.close()

    pb = lib.get_playback("movie", movie_id)
    assert pb["position_sec"] == 31
    assert pb["duration_sec"] == 3600
    assert pb["watched"] == 0  # 31/3600 远未到 90%，剩余远大于 3 分钟


def test_90pct_marks_watched(tmp_path, cfg, lib, sock_dir):
    movie_id = seed_movie(tmp_path, cfg, lib)
    captured = {}
    player = make_player(cfg, lib, captured)
    sock = sock_dir / "play.sock"
    server = FakeMpvServer(sock, time_pos_seq=[95], duration=100)
    try:
        player.play("movie", movie_id, sock_path=sock)
        wait_until(lambda: lib.get_playback("movie", movie_id))
        player.wait_all(timeout=5)
    finally:
        server.close()

    pb = lib.get_playback("movie", movie_id)
    assert pb["position_sec"] == 95 and pb["watched"] == 1
    # 已看后下次从头播
    assert player._start_position(pb) == 0.0


def test_remaining_under_3min_marks_watched(tmp_path, cfg, lib, sock_dir):
    movie_id = seed_movie(tmp_path, cfg, lib)
    captured = {}
    player = make_player(cfg, lib, captured)
    sock = sock_dir / "play.sock"
    server = FakeMpvServer(sock, time_pos_seq=[3540], duration=3600)  # 剩 60 秒
    try:
        player.play("movie", movie_id, sock_path=sock)
        wait_until(lambda: lib.get_playback("movie", movie_id))
        player.wait_all(timeout=5)
    finally:
        server.close()
    assert lib.get_playback("movie", movie_id)["watched"] == 1


def test_socket_closed_ends_thread_and_keeps_last_position(tmp_path, cfg, lib, sock_dir):
    movie_id = seed_movie(tmp_path, cfg, lib)
    captured = {}
    player = make_player(cfg, lib, captured)
    sock = sock_dir / "play.sock"
    server = FakeMpvServer(sock, time_pos_seq=[30], duration=3600)  # 返回一次即断开
    try:
        player.play("movie", movie_id, sock_path=sock)
        wait_until(lambda: lib.get_playback("movie", movie_id))
        player.wait_all(timeout=5)  # 线程应在断开后退出
    finally:
        server.close()

    assert all(not t.is_alive() for t in player._threads)
    pb = lib.get_playback("movie", movie_id)
    assert pb["position_sec"] == 30  # 最后一次位置已落盘


def test_resume_from_last_position(tmp_path, cfg, lib, sock_dir):
    movie_id = seed_movie(tmp_path, cfg, lib)
    seed_playback(lib, movie_id, 500, 1000, False)
    captured = {}
    player = make_player(cfg, lib, captured)
    sock = sock_dir / "play.sock"
    server = FakeMpvServer(sock, time_pos_seq=[505], duration=1000)
    try:
        result = player.play("movie", movie_id, sock_path=sock)
        assert result["start_at"] == 500
        assert "--mpv-start=500" in captured["cmd"]
        player.wait_all(timeout=5)
    finally:
        server.close()


def test_under_30s_counts_as_unwatched(tmp_path, cfg, lib, sock_dir):
    movie_id = seed_movie(tmp_path, cfg, lib)
    seed_playback(lib, movie_id, 10, 1000, False)
    captured = {}
    player = make_player(cfg, lib, captured)
    sock = sock_dir / "play.sock"
    server = FakeMpvServer(sock, time_pos_seq=[15], duration=1000)
    try:
        result = player.play("movie", movie_id, sock_path=sock)
        assert result["start_at"] == 0.0
        assert "--mpv-start=0" in captured["cmd"]
        player.wait_all(timeout=5)
    finally:
        server.close()


def test_watched_restarts_from_zero(tmp_path, cfg, lib, sock_dir):
    movie_id = seed_movie(tmp_path, cfg, lib)
    seed_playback(lib, movie_id, 950, 1000, True)
    captured = {}
    player = make_player(cfg, lib, captured)
    sock = sock_dir / "play.sock"
    server = FakeMpvServer(sock, time_pos_seq=[5], duration=1000)
    try:
        result = player.play("movie", movie_id, sock_path=sock)
        assert result["start_at"] == 0.0
        player.wait_all(timeout=5)
    finally:
        server.close()


def test_play_episode(tmp_path, cfg, lib, sock_dir):
    tv = tmp_path / "tv"
    show_dir = tv / "Sample Show"
    (show_dir / "Season 01").mkdir(parents=True)
    (show_dir / "Season 01" / "Sample Show S01E01.mkv").write_bytes(b"")
    run_scan(cfg, lib)
    ep = lib.episodes_for_show(lib.list_shows()[0]["id"])[0]

    captured = {}
    player = make_player(cfg, lib, captured)
    sock = sock_dir / "play.sock"
    server = FakeMpvServer(sock, time_pos_seq=[7], duration=2700)
    try:
        result = player.play("episode", ep["id"], sock_path=sock)
        assert result["ok"] is True
        player.wait_all(timeout=5)
    finally:
        server.close()
    pb = lib.get_playback("episode", ep["id"])
    assert pb["position_sec"] == 7


def test_missing_file_refused(tmp_path, cfg, lib, sock_dir):
    import pytest
    from homecinema.player import PlayerError
    movie_id = seed_movie(tmp_path, cfg, lib)
    lib._conn.execute("UPDATE movies SET missing=1")
    lib._conn.commit()
    captured = {}
    player = make_player(cfg, lib, captured)
    with pytest.raises(PlayerError):
        player.play("movie", movie_id, sock_path=sock_dir / "play.sock")


def test_socket_never_appears_thread_gives_up(tmp_path, cfg, lib, sock_dir):
    """等不到 IPC socket：不写任何进度，线程退出。"""
    movie_id = seed_movie(tmp_path, cfg, lib)
    captured = {}
    player = make_player(cfg, lib, captured)
    player.play("movie", movie_id, sock_path=sock_dir / "play.sock")  # 没有服务端
    player.wait_all(timeout=10)
    assert all(not t.is_alive() for t in player._threads)
    assert lib.get_playback("movie", movie_id) is None
