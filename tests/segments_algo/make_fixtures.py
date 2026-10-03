"""用 ffmpeg 合成测试用的「电视剧」音频，输出到 fixtures/。

三组素材（44100Hz 单声道 wav，全部由 ffmpeg 的 aevalsrc 正弦 + random() 噪声合成）：
- ep1..ep4.wav  300 秒：各集长度不同的冷开场 + 各集完全相同的 45 秒片头曲 + 各集完全不同的正片。
- cr1..cr4.wav  300 秒：正片 + 各集完全相同的 60 秒片尾曲（起点各集不同）+ 结尾几秒不同内容。
- ns1..ns3.wav  200 秒：三集内容互不相同，没有任何共同片段。

共用的片头曲/片尾曲只渲染一次（同一份 wav 拼进每集），所以各集那一段声纹逐位相同。
「歌曲」= 每 3 秒换一个音高的旋律（基频 + 2/3 次谐波 + 低八度 + 每音符包络 + 噪声瞬态），
不是单一正弦波，频谱持续变化，声纹区分度够。
「各集专属内容」= 一串随机音符（音高、音符时长、谐波配比、噪声比例都随机），逐音符渲染再拼接，
这样各集非共同部分在声纹上互不相似（否则会被误当成共同片段）。
注意：不要留静音——chromaprint 会丢掉静音帧，声纹整数个数与秒数就不再线性对应。

用法：/opt/homebrew/bin/python3 make_fixtures.py [--force]
"""

from __future__ import annotations

import math
import os
import random
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE_DIR = os.path.join(HERE, "fixtures")
FFMPEG = "/opt/homebrew/bin/ffmpeg" if os.path.exists("/opt/homebrew/bin/ffmpeg") else "ffmpeg"
SAMPLE_RATE = 44100

INTRO_LEN = 45.0
CREDITS_LEN = 60.0
EPISODE_LEN = 300.0
NO_SHARED_LEN = 200.0

# 各集冷开场长度（秒）。片头曲真值 = (COLD_OPEN[i], COLD_OPEN[i] + INTRO_LEN)
COLD_OPEN = [5.0, 20.0, 0.0, 40.0]
# 各集片尾曲起点（秒）。片尾曲真值 = (CREDITS_START[i], CREDITS_START[i] + CREDITS_LEN)
CREDITS_START = [200.0, 210.0, 220.0, 230.0]

# 各集共用的片头曲 / 片尾曲：音高轨迹 = mod(floor(t/note_sec)*p + s, m)，参数固定
INTRO_SONG = dict(note_sec=3.0, lo=180, hi=1300, p=17, s=5, m=97)
CREDITS_SONG = dict(note_sec=3.0, lo=150, hi=900, p=29, s=11, m=101)

# 各集「专属内容」取料区间：音区 + 音符时长范围 + 随机种子（每集拉开差距）
EP_CONTENT = [
    dict(lo=90, hi=520, min_note=7.0, max_note=17.0, seed="coldbody-A"),
    dict(lo=320, hi=2600, min_note=3.0, max_note=9.0, seed="coldbody-B"),
    dict(lo=130, hi=900, min_note=9.0, max_note=21.0, seed="coldbody-C"),
    dict(lo=700, hi=4200, min_note=2.0, max_note=5.0, seed="coldbody-D"),
]
NO_SHARED_CONTENT = [
    dict(lo=100, hi=600, min_note=8.0, max_note=18.0, seed="ns-A"),
    dict(lo=800, hi=3800, min_note=2.5, max_note=6.0, seed="ns-B"),
    dict(lo=250, hi=1200, min_note=11.0, max_note=24.0, seed="ns-C"),
]

INTRO_TRUTH = {f"ep{i + 1}": (c, c + INTRO_LEN) for i, c in enumerate(COLD_OPEN)}
CREDITS_TRUTH = {f"cr{i + 1}": (s, s + CREDITS_LEN) for i, s in enumerate(CREDITS_START)}
NO_SHARED_KEYS = ["ns1", "ns2", "ns3"]
NO_AUDIO_NAME = "no_audio.mp4"  # 只有视频流的文件，用来测「没有音轨」的分支


def _run(cmd: list[str]) -> None:
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"命令失败: {' '.join(cmd)}\n{res.stderr[-2000:]}")


def song_expr(note_sec: float, lo: int, hi: int, p: int, s: int, m: int,
              gain: float = 0.28, h2: float = 0.45, h3: float = 0.22) -> str:
    """一段共用「歌曲」的 ffmpeg 表达式：变调旋律 + 谐波 + 低八度 + 每音符包络 + 噪声。"""
    k = f"floor(t/{note_sec})"
    freq = f"({lo}+{hi - lo}*mod({k}*{p}+{s},{m})/{m})"
    env = f"(0.45+0.55*exp(-1.6*mod(t,{note_sec})))"
    lead = f"{gain}*{env}*(sin(2*PI*{freq}*t)+{h2}*sin(4*PI*{freq}*t)+{h3}*sin(6*PI*{freq}*t))"
    bass = f"{gain * 0.8:.4f}*sin(PI*{freq}*t)"
    attack = f"0.10*random(0)*lt(mod(t,{note_sec}),{min(0.12, note_sec / 8):.4f})"
    hiss = "0.03*random(1)*(0.6+0.4*sin(2*PI*0.5*t))"
    return f"min(1,max(-1,{lead}+{bass}+{attack}+{hiss}))"


def note_expr(rng: random.Random, freq: float) -> str:
    """单个音符的 ffmpeg 表达式：随机谐波配比 + 随机颤音 + 随机起音/底噪。"""
    f = (f"{freq:.2f}*(1+{rng.uniform(0.004, 0.03):.4f}"
         f"*sin(2*PI*{rng.uniform(0.7, 7.0):.2f}*t))")
    partials = [f"sin(2*PI*{f}*t)", f"{rng.uniform(0.1, 0.8):.3f}*sin(4*PI*{f}*t)"]
    for mult in (6, 8):
        partials.append(f"{rng.uniform(0.0, 0.5):.3f}*sin({mult}*PI*{f}*t)")
    tone = (f"{rng.uniform(0.14, 0.28):.4f}*(0.35+0.65*exp(-{rng.uniform(0.2, 1.6):.3f}*t))"
            f"*({'+'.join(partials)})")
    noise = (f"{rng.uniform(0.01, 0.13):.4f}*random(0)"
             f"+{rng.uniform(0.0, 0.06):.4f}*random(1)"
             f"*(0.5+0.5*sin(2*PI*{rng.uniform(0.3, 3.0):.2f}*t))")
    attack = f"{rng.uniform(0.05, 0.22):.4f}*random(2)*exp(-{rng.uniform(10, 45):.1f}*t)"
    return f"min(1,max(-1,{tone}+{noise}+{attack}))"


def _render(expr: str, duration: float, path: str) -> str:
    _run([
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi",
        "-i", f"aevalsrc='{expr}':d={duration:.6f}:c=1:s={SAMPLE_RATE}",
        "-c:a", "pcm_s16le", "-ar", str(SAMPLE_RATE), "-ac", "1", path,
    ])
    return path


def _concat(parts: list[str], out: str) -> str:
    list_path = out + ".list.txt"
    with open(list_path, "w") as fh:
        for part in parts:
            fh.write(f"file '{os.path.abspath(part)}'\n")
    _run([
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "concat", "-safe", "0", "-i", list_path,
        "-c:a", "pcm_s16le", "-ar", str(SAMPLE_RATE), "-ac", "1", out,
    ])
    os.remove(list_path)
    return out


def render_unique(total: float, content: dict, tag: str, out_dir: str) -> str:
    """渲染 total 秒「本集专属」内容：一串随机音符，逐音符渲染再拼接。"""
    rng = random.Random(f"{tag}:{content['seed']}:{total:.3f}")
    parts = []
    remaining = total
    index = 0
    while remaining > 0.05:
        if remaining < content["min_note"]:
            dur = remaining
        else:
            dur = min(remaining, rng.uniform(content["min_note"], content["max_note"]))
        freq = math.exp(rng.uniform(math.log(content["lo"]), math.log(content["hi"])))
        path = os.path.join(out_dir, f"{tag}_{index:03d}.wav")
        parts.append(_render(note_expr(rng, freq), dur, path))
        remaining -= dur
        index += 1
    return _concat(parts, os.path.join(out_dir, f"{tag}_full.wav"))


def fixture_path(name: str) -> str:
    return os.path.join(FIXTURE_DIR, name)


def build_fixtures(force: bool = False) -> str:
    """生成全部素材到 fixtures/，返回目录；已存在且 force=False 时跳过该组素材。"""
    os.makedirs(FIXTURE_DIR, exist_ok=True)
    tmp = os.path.join(FIXTURE_DIR, "_parts")
    os.makedirs(tmp, exist_ok=True)

    def fresh(name: str) -> bool:
        return force or not os.path.exists(os.path.join(FIXTURE_DIR, name))

    def shared_song(name: str, spec: dict, duration: float) -> str:
        expr = song_expr(spec["note_sec"], spec["lo"], spec["hi"], spec["p"],
                         spec["s"], spec["m"], gain=0.28)
        return _render(expr, duration, os.path.join(tmp, f"{name}.wav"))

    intro = shared_song("intro", INTRO_SONG, INTRO_LEN)
    credits = shared_song("credits", CREDITS_SONG, CREDITS_LEN)

    if fresh("ep1.wav"):
        for i, cold in enumerate(COLD_OPEN):
            content = EP_CONTENT[i]
            parts = []
            if cold > 0:
                parts.append(render_unique(cold, content, f"ep{i}_cold", tmp))
            parts.append(intro)
            parts.append(render_unique(EPISODE_LEN - cold - INTRO_LEN, content,
                                       f"ep{i}_body", tmp))
            _concat(parts, fixture_path(f"ep{i + 1}.wav"))

    if fresh("cr1.wav"):
        for i, start in enumerate(CREDITS_START):
            content = EP_CONTENT[i]
            parts = [
                render_unique(start, content, f"cr{i}_pre", tmp),
                credits,
                render_unique(EPISODE_LEN - start - CREDITS_LEN, content, f"cr{i}_tail", tmp),
            ]
            _concat(parts, fixture_path(f"cr{i + 1}.wav"))

    if fresh("ns1.wav"):
        for i in range(3):
            part = render_unique(NO_SHARED_LEN, NO_SHARED_CONTENT[i], f"ns{i}", tmp)
            _concat([part], fixture_path(f"ns{i + 1}.wav"))

    if fresh(NO_AUDIO_NAME):
        # 只有视频轨、没有音轨的文件
        _run([
            FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "color=c=black:s=64x64:d=5:r=10",
            "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", fixture_path(NO_AUDIO_NAME),
        ])

    for name in os.listdir(tmp):
        os.remove(os.path.join(tmp, name))
    try:
        os.rmdir(tmp)
    except OSError:
        pass
    return FIXTURE_DIR


if __name__ == "__main__":
    target = build_fixtures(force="--force" in sys.argv)
    print("fixtures 目录:", target)
    for entry in sorted(os.listdir(target)):
        print("  ", entry, os.path.getsize(os.path.join(target, entry)), "bytes")
