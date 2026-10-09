"""回归：2026-10-03 ZCode GLM-5.3-Flash 审计 player.py/detect.py 找到的 4 个 bug（修复前全部失败）。

每条发现一个 test_FXX_*，断言的是**正确行为**，所以在当前代码上应当失败（这就是复现）。
播放器用脚本化假 mpv IPC server，detect 用假 ffprobe/假声纹；
不启动任何真实播放器、不调真 ffmpeg、不访问外网。
"""

from __future__ import annotations

import json
import socket
import threading

from homecinema import detect
from homecinema.player import Player
from homecinema.scanner import run_scan


class ScriptedMpvServer:
    """模拟 mpv JSON IPC：按脚本逐条应答 get_property。

    script 里每个动作对应收到的每一条命令：
      ("ok", value)   正常应答 {"error": "success", "data": value}
      ("raw", bytes)  原样写回字节（构造半行 / 粘包）
      ("close",)      收到这条命令后直接断开连接（模拟播放器退出）
      ("silent",)     保持连接但永不回包
    脚本耗尽后再收到命令默认断开。
    """

    def __init__(self, sock_path, script, timers=None):
        self.sock_path = str(sock_path)
        self.script = list(script)
        self.timers = list(timers or [])  # (delay_sec, bytes)：accept 后定时补发，不占命令配额
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

    @staticmethod
    def _send_later(conn, delay, payload):
        def fire():
            try:
                conn.sendall(payload)
            except OSError:
                pass
        t = threading.Timer(delay, fire)
        t.daemon = True
        t.start()

    def _handle(self, conn):
        for delay, payload in self.timers:
            self._send_later(conn, delay, payload)
        reader = conn.makefile("rb")
        try:
            while True:
                line = reader.readline()
                if not line:
                    break
                self.commands.append(line)
                action = self.script.pop(0) if self.script else ("close",)
                kind = action[0]
                if kind == "ok":
                    conn.sendall(
                        json.dumps({"error": "success", "data": action[1]}).encode() + b"\n")
                elif kind == "raw":
                    conn.sendall(action[1])
                elif kind == "close":
                    break
                elif kind == "silent":
                    while self._running:
                        threading.Event().wait(0.05)
                    break
        finally:
            reader.close()
            conn.close()

    def close(self):
        self._running = False
        try:
            self._srv.close()
        except OSError:
            pass


def make_player(cfg, lib, captured, **kwargs):
    def launcher(cmd, stdin=None, stdout=None, stderr=None):
        captured["cmd"] = list(cmd)
        captured["stdin"] = stdin

    opts = dict(poll_interval=0.05, connect_deadline=3.0)
    opts.update(kwargs)
    return Player(lib, cfg, launcher=launcher, **opts)


def seed_movie(tmp_path, cfg, lib):
    folder = tmp_path / "movies" / "Example Movie (2001)"
    folder.mkdir(parents=True)
    (folder / "Example Movie (2001).mp4").write_bytes(b"")
    run_scan(cfg, lib)
    return lib.list_movies()[0]["id"]


# ---------- 发现 ----------

def test_F01_fresh_position_lost_when_disconnect_between_two_requests(tmp_path, cfg, lib, sock_dir):
    """F01：断开发生在 time-pos 与 duration 两次请求之间时，刚取到的新位置被整条丢弃。

    服务端正常应答一次 time-pos=600，随后不再应答 duration、直接断开。
    正确行为：手里已有的最新位置应在监控线程退出前落库（时长未知就存 None）。
    （player.py 末尾注释声称「最后一次位置在断开前已写入」，当前实现做不到。）"""
    movie_id = seed_movie(tmp_path, cfg, lib)
    captured = {}
    player = make_player(cfg, lib, captured)
    sock = sock_dir / "play.sock"
    server = ScriptedMpvServer(sock, script=[("ok", 600), ("close",)])
    try:
        player.play("movie", movie_id, sock_path=sock)
        player.wait_all(timeout=5)
    finally:
        server.close()

    pb = lib.get_playback("movie", movie_id)
    assert pb is not None, "断开前已取到的 time-pos=600 未落库（整条进度丢失）"
    assert pb["position_sec"] == 600


def test_F02_silent_peer_blocks_monitor_thread_forever(tmp_path, cfg, lib, sock_dir):
    """F02：连接建立但对端收到请求后永不回包（mpv 卡死/假 IPC 服务），监控线程永久阻塞。

    正确行为：连接后无响应也应有放弃机制（同「等不到 socket」有 connect_deadline 一样），
    超时后退出，active_count 归零；不能让线程卡在 readline 上永不退出。
    测试给 2 秒宽限（远大于轮询间隔 0.05s，远小于 startup_grace 120s）。"""
    movie_id = seed_movie(tmp_path, cfg, lib)
    captured = {}
    player = make_player(cfg, lib, captured)
    sock = sock_dir / "play.sock"
    server = ScriptedMpvServer(sock, script=[("silent",)])
    try:
        player.play("movie", movie_id, sock_path=sock)
        player.wait_all(timeout=2.0)
        alive = [t.name for t in player._threads if t.is_alive()]
        assert not alive, f"对端永不回包时监控线程永久阻塞：{alive}"
    finally:
        server.close()


def test_F03_progress_report_without_duration_wipes_known_duration(tmp_path, cfg, lib):
    """F03：save_progress 收到 duration=None 的进度上报时，把库里已知时长抹成 NULL。

    触发路径是真实的：macapp/main.swift 的 postProgress / postFinalProgressSync 在
    currentDuration 为 nil 时都发 "duration": NSNull()（mpv 还没读到时长、或退出前
    最后一次上报）。正确行为：时长未提供时沿用库里已知值，已看判定也基于它计算，
    而不是把已知数据抹掉、把已看降级。"""
    movie_id = seed_movie(tmp_path, cfg, lib)
    # 上一轮已看到 3420/3600，已标已看
    lib.save_playback("movie", movie_id, 3420.0, 3600.0, True)
    player = make_player(cfg, lib, {})

    player.save_progress("movie", movie_id, 3500.0, None)  # duration 未知

    pb = lib.get_playback("movie", movie_id)
    assert pb["duration_sec"] == 3600.0, "duration 缺失的上报把库里已知时长抹成 NULL"
    assert pb["watched"] == 1, "时长被抹掉后已看判定失效，已看被降级成未看"


def _seed_show(tmp_path, cfg, lib, count=2):
    show_dir = tmp_path / "tv" / "Sample Show" / "Season 01"
    show_dir.mkdir(parents=True)
    for n in range(1, count + 1):
        (show_dir / f"Sample Show S01E{n:02d}.mkv").write_bytes(b"")
    run_scan(cfg, lib)
    return lib.episodes_for_show(lib.list_shows()[0]["id"])


def test_F04_misshaped_range_from_segments_aborts_whole_batch(tmp_path, cfg, lib, monkeypatch):
    """F04：segments.detect_season 返回形状错配的区间值（如 (None, None)）时，
    _as_range 的 float() 不在任何保护范围内，TypeError 一路炸穿 _fingerprint_pass、
    run_detect_segments，整批检测中断。

    detect.py 模块文档明确承诺「形状对不上会落进『没把握』」、detect_season 的
    文档承诺「单集出错按 none 计，不中断」、_detect 的文档承诺「异常都返回空 dict
    （整段按 none 继续）」。正确行为：错配值按 None 处理，整季照常落库。"""
    eps = _seed_show(tmp_path, cfg, lib)

    monkeypatch.setattr(detect, "probe_duration", lambda path: 1200.0)
    monkeypatch.setattr(detect, "probe_chapters", lambda path: [])
    monkeypatch.setattr(detect.segments, "chapters_to_segments",
                        lambda chapters, duration: {"intro": None, "credits": None})
    monkeypatch.setattr(detect.segments, "fingerprint", lambda path, start, length: [1, 2, 3])
    # 形状错配：把「没有结果」编码成 (None, None) 而不是 None
    monkeypatch.setattr(detect.segments, "detect_season",
                        lambda fps, offset_sec=0.0, **kw: {k: (None, None) for k in fps})

    # 不应抛异常
    detect.run_detect_segments(lib, progress=lambda msg, **kw: None)

    for ep in eps:
        row = lib.get_segments(ep["id"])
        assert row is not None, "形状错配导致整批中断，该集没有落库"
        assert row["intro_start"] is None and row["credits_start"] is None
        assert row["source"] == "none"

