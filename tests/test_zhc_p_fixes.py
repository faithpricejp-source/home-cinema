"""Kimi 审查复核（P 路 7 条；P-2 判 needs_decision 未合，其测试已剔除）的复现与回归测试。

每条发现对应直接调用真文件里真函数/真脚本的测试：修复前应失败，修复后通过。
判定依据与修复前后输出摘录见 ../VERDICT.md 各条。
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
AUTO_SCAN = PROJECT / "tools" / "auto_scan.sh"
REAL_LOCK = Path("/tmp/homecinema-autoscan.lock")


def load_tool(name: str):
    """按文件路径加载 tools/<name>.py（tools 不是包），与 test_audit_today_tools 同法。"""
    spec = importlib.util.spec_from_file_location(name, PROJECT / "tools" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------- P-1: auto_scan.sh 残留锁静默停摆 ----------

def run_auto_scan(lock: Path) -> subprocess.CompletedProcess:
    """跑真的 auto_scan.sh：source 进一个把 sleep 换成自杀的 zsh。

    媒体目录存在，脚本一旦过了锁就会 sleep 90 后 cd 进
    真仓扫描；把 sleep 换成 kill -9 $$ 等同于持锁中被 SIGKILL，脚本最多活到
    sleep 那一行，永远碰不到 cd/scan。AUTOSCAN_LOCK 只有修复版才认，用于把锁
    指到临时目录（launchd 不设它，生产行为不变）。
    """
    src = (
        "sleep() { kill -9 $$ }\n"
        f"AUTOSCAN_LOCK={lock}\n"
        f". {AUTO_SCAN}\n"
    )
    return subprocess.run(["/bin/zsh", "-c", src], capture_output=True, text=True,
                          timeout=30)


@pytest.fixture
def guard_real_lock():
    """修复前的脚本不认 AUTOSCAN_LOCK、仍操作真 /tmp 锁：记录原状，
    测试后若是新出现的残留就清掉，绝不往真机留垃圾。"""
    existed = REAL_LOCK.exists()
    yield
    if not existed and REAL_LOCK.exists():
        shutil.rmtree(REAL_LOCK, ignore_errors=True)


@pytest.fixture
def live_pid():
    p = subprocess.Popen(["/bin/sleep", "60"])
    yield p.pid
    p.kill()
    p.wait()


def test_p1_stale_lock_without_pid_is_reclaimed(tmp_path, guard_real_lock):
    lock = tmp_path / "autoscan.lock"
    lock.mkdir()  # 模拟持锁中被 SIGKILL 的残留（旧脚本连 pid 都不写）
    out = run_auto_scan(lock)
    assert "another scan running" not in out.stdout
    # 残留锁必须被收走：要么整个被清（正常走完），要么至少已重写持锁 pid
    assert (not lock.exists()) or (lock / "pid").exists()


def test_p1_live_lock_is_respected(tmp_path, live_pid, guard_real_lock):
    lock = tmp_path / "autoscan.lock"
    lock.mkdir()
    (lock / "pid").write_text(f"{live_pid}\n")
    out = run_auto_scan(lock)
    assert "skip: another scan running" in out.stdout  # 活锁要让位
    assert (lock / "pid").read_text().strip() == str(live_pid)  # 且不许偷活锁


def test_p1_stale_lock_by_age_is_reclaimed(tmp_path, live_pid, guard_real_lock):
    lock = tmp_path / "autoscan.lock"
    lock.mkdir()
    (lock / "pid").write_text(f"{live_pid}\n")
    old = time.time() - 7 * 3600  # 锁龄 7 小时 > 兜底触发周期（launchd 6 小时）
    os.utime(lock, (old, old))
    out = run_auto_scan(lock)
    assert "another scan running" not in out.stdout
    # pid 活着但锁龄超限：PID 可能已被复用，必须按残留处理重抢
    assert not lock.exists() or (lock / "pid").read_text().strip() != str(live_pid)


# ---------- P-3 / P-5: vlm_credits.py sheet_for ----------

def _vlm_env(tmp_path, monkeypatch, duration):
    """给真 sheet_for 造可跑的环境：临时库 + 临时拼图目录 + 假 probe_duration，
    绝不碰模块默认的 ~/Library 路径。返回 (模块, 视频文件路径)；
    build_sheet 由各测试自行注入。"""
    mod = load_tool("vlm_credits")
    db = tmp_path / "lib.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE episodes (id INTEGER PRIMARY KEY, path TEXT)")
    vid = tmp_path / "ep1.mp4"
    vid.write_bytes(b"\x00" * 16)  # 真实存在，供 os.stat 读 mtime/size
    con.execute("INSERT INTO episodes VALUES (1, ?)", (str(vid),))
    con.commit()
    con.close()
    monkeypatch.setattr(mod, "DB", str(db))
    monkeypatch.setattr(mod, "SHEETS", str(tmp_path / "sheets"))
    monkeypatch.setattr(mod, "probe_duration", lambda p: duration)
    return mod, vid


def test_p3_sheet_for_none_duration_raises_clear_error(tmp_path, monkeypatch):
    """probe_duration 对损坏/不支持的文件合法返回 None（homecinema/detect.py:33-39），
    sheet_for 必须显式报错，而不是把 None 递进 build_sheet 抛 TypeError。"""
    mod, _vid = _vlm_env(tmp_path, monkeypatch, duration=None)
    with pytest.raises(RuntimeError, match="时长"):
        mod.sheet_for(1)


def test_p4_run_batch_tolerates_truncated_results_line(tmp_path, monkeypatch, capsys):
    """vlm_results.jsonl 是 append-only 非原子写，被杀会留残行/空行；
    续跑加载时坏行要跳过（残行没有完整 eid，跳过=该集下次重判，无损），
    不能让整个批处理启动即崩。"""
    mod, _vid = _vlm_env(tmp_path, monkeypatch, duration=600.0)
    results = tmp_path / "vlm_results.jsonl"
    results.write_text(
        '{"eid": 5, "credits": 100.0, "k": 3, "n": 50, "reason": "ok"}\n'
        '{"eid": 7, "cre\n'  # 上次写一半被杀的残行
        '\n',                # 空行
        encoding="utf-8")
    monkeypatch.setattr(mod, "RESULTS", str(results))
    monkeypatch.setattr(mod, "batch_targets", lambda: [5, 7])
    monkeypatch.setattr(mod, "sheet_for", lambda eid: ("img.jpg", 3.0, 50, 600.0))
    monkeypatch.setattr(mod, "ask", lambda model, img, retries=3:
                        {"first_non_story": 3, "reason": "test"})

    assert mod.run_batch() == 0
    assert "待判 1 集（已判 1）" in capsys.readouterr().out  # 5 已判，7 的残行不算数 → 重判
    recs = []
    for l in results.read_text(encoding="utf-8").splitlines():
        try:  # 残行仍在盘上（读侧容忍，不重写文件），解析时同样跳过
            recs.append(json.loads(l))
        except json.JSONDecodeError:
            continue
    assert [r["eid"] for r in recs] == [5, 7]  # 重判后 7 的完整结果追加落盘
    assert recs[1]["credits"] == pytest.approx(21.0)  # credits_from_answer: 3.0 + 3*6


def test_p5_sheet_cache_invalidated_when_file_changes(tmp_path, monkeypatch):
    """拼图缓存原先只按集 id 键控：视频被重新压制/替换后（集 id 不变）会永远
    复用旧拼图，让 VLM 对旧版本判片尾。meta 必须记 mtime/size/时长并逐一比对，
    对不上就重建；meta 写入要原子（写一半被杀的残缺 meta 当未命中重建）。"""
    mod, vid = _vlm_env(tmp_path, monkeypatch, duration=600.0)
    calls = []

    def fake_build(path, duration, out):
        calls.append(path)
        Path(out).write_bytes(b"fake-jpg")  # 真代码只判存在性，占位即可
        return 3.0, 50

    monkeypatch.setattr(mod, "build_sheet", fake_build)

    mod.sheet_for(1)
    mod.sheet_for(1)
    assert len(calls) == 1  # 前置：同文件二次调用走缓存，不重建

    with open(vid, "ab") as fh:  # 文件被换（重新压制/换版本）：mtime、size 都变
        fh.write(b"replaced")
    mod.sheet_for(1)
    assert len(calls) == 2  # 修前仍是 1：命中旧缓存，拿旧拼图判新文件

    monkeypatch.setattr(mod, "probe_duration", lambda p: 900.0)  # 时长也变了
    mod.sheet_for(1)
    assert len(calls) == 3  # 修前仍是 1：duration 只透传、不参与缓存校验

    # 残缺 meta（写一半被杀）：当未命中重建，不能 json.load 崩掉
    meta = Path(mod.SHEETS) / "1.jpg.json"
    meta.write_text('{"t0": 3.0, "n": ', encoding="utf-8")
    mod.sheet_for(1)
    assert len(calls) == 4


def test_p6_eval_continues_past_single_episode_failure(tmp_path, monkeypatch, capsys):
    """eval <模型> <集id>... 里 sheet_for 原先在 try 之外：一集取拼图失败
    （文件被移走 / id 不存在 / P-3 的时长 None）会 traceback 掉整轮 eval，
    排在后面的集全部得不到结果。必须与 run_batch 一样逐集容错。"""
    mod, _vid = _vlm_env(tmp_path, monkeypatch, duration=600.0)

    def fake_sheet(eid):
        if eid == 11:
            raise RuntimeError("文件没了")
        return ("img.jpg", 3.0, 50, 600.0)

    monkeypatch.setattr(mod, "sheet_for", fake_sheet)
    monkeypatch.setattr(mod, "ask", lambda model, img, retries=3:
                        {"first_non_story": 2, "reason": "r"})
    monkeypatch.setattr(sys, "argv", ["vlm_credits.py", "eval", "m", "11", "12"])

    assert mod.main() == 0
    lines = [json.loads(l) for l in capsys.readouterr().out.strip().splitlines()]
    assert lines[0] == {"eid": 11, "error": "文件没了"}  # 失败集只记一行错误
    assert lines[1]["eid"] == 12 and lines[1]["k"] == 2  # 后面的集照常出结果


# ---------- P-7: merge_segments.py load_vlm 对坏行无容错 ----------

def test_p7_load_vlm_skips_bad_lines(tmp_path, monkeypatch):
    """vlm_results.jsonl 的唯一写入方是 vlm_credits 的 append（非原子），被杀留残行；
    load_vlm 遇残行/空行必须跳过，否则 plan() 崩溃、--write 一个字也写不进，
    与 P-4 同时卡死「继续判」和「合并入库」两个环节。"""
    mod = load_tool("merge_segments")
    vlm = tmp_path / "vlm_results.jsonl"
    vlm.write_text(
        '{"eid": 5, "credits": 120.5, "k": 3, "n": 50, "reason": "ok"}\n'
        '{"eid": 6, "cre\n'               # 上次写一半被杀的残行
        '\n'                              # 空行
        '{"eid": 7, "credits": null}\n',  # 判了但没有片尾结论的行
        encoding="utf-8")
    monkeypatch.setattr(mod, "VLM", str(vlm))
    assert mod.load_vlm() == {5: pytest.approx(120.5)}  # 6 残行跳过、7 的 null 本就不收
