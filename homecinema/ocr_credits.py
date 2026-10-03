"""用画面文字密度认片尾：取每集最后若干分钟，每 STEP_SEC 秒截一帧，用 macOS Vision 数文字。

演职员表是成屏的人名，从片尾开始文字行数陡增并一直持续到结束（或到片尾后的预告/台标）。
声纹认不出的集（片尾配乐每集不同）用这个补。

分两步：extract() 截帧+识别，每集特征缓存成 JSON（调规则不用重新截帧）；
decide() 只读特征给出片尾起点。截帧在临时目录，不出本机。
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile

TAIL_SEC = 300.0      # 只看最后 5 分钟（标准答案里片尾起点离结尾最远 188 秒）
STEP_SEC = 2.0        # 截帧间隔
FRAME_WIDTH = 960
DARK_LUMA = 40        # 灰度低于此值算「暗像素」


# ---------- 截帧与识别 ----------

def extract(path: str, duration: float, tail_sec: float = TAIL_SEC,
            step: float = STEP_SEC) -> dict:
    """返回 {"duration", "start", "step", "frames": [{"t", "dark", "lines": [[text, x, y, w, h]...]}]}。"""
    start = max(0.0, duration - tail_sec)
    with tempfile.TemporaryDirectory(prefix="hc-ocr-") as tmp:
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
               "-hwaccel", "videotoolbox", "-ss", f"{start:.3f}", "-i", path,
               "-an", "-sn", "-vf", f"fps=1/{step},scale={FRAME_WIDTH}:-2",
               "-q:v", "4", os.path.join(tmp, "f%05d.jpg")]
        subprocess.run(cmd, check=True, timeout=900, capture_output=True)
        names = sorted(n for n in os.listdir(tmp) if n.endswith(".jpg"))
        frames = []
        for i, name in enumerate(names):
            image = os.path.join(tmp, name)
            # fps 滤镜第 i 帧对应 start + (i + 0.5) * step 附近
            frames.append({"t": round(start + (i + 0.5) * step, 2),
                           "dark": round(_dark_fraction(image), 3),
                           "lines": recognize(image)})
    return {"duration": duration, "start": start, "step": step, "frames": frames}


def _dark_fraction(image_path: str) -> float:
    import numpy as np
    from PIL import Image
    with Image.open(image_path) as im:
        gray = np.asarray(im.convert("L").resize((160, 90)))
    return float((gray < DARK_LUMA).mean())


def recognize(image_path: str) -> list[list]:
    """Vision 快速档识别，返回 [[文字, x, y, w, h], ...]（坐标归一化，原点在左下）。"""
    import Quartz
    import Vision
    from Foundation import NSURL

    src = Quartz.CGImageSourceCreateWithURL(NSURL.fileURLWithPath_(image_path), None)
    if src is None:
        return []
    img = Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)
    req = Vision.VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelFast)
    req.setUsesLanguageCorrection_(False)
    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(img, None)
    ok, _err = handler.performRequests_error_([req], None)
    if not ok:
        return []
    lines = []
    for obs in req.results() or []:
        cand = obs.topCandidates_(1)
        if not cand:
            continue
        b = obs.boundingBox()
        lines.append([str(cand[0].string()), round(b.origin.x, 3), round(b.origin.y, 3),
                      round(b.size.width, 3), round(b.size.height, 3)])
    return lines


def load_or_extract(cache_dir: str, episode_id: int, path: str, duration: float) -> dict:
    os.makedirs(cache_dir, exist_ok=True)
    cache = os.path.join(cache_dir, f"{episode_id}.json")
    if os.path.exists(cache):
        with open(cache, encoding="utf-8") as fh:
            return json.load(fh)
    data = extract(path, duration)
    tmp = cache + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    os.replace(tmp, cache)
    return data


# ---------- 判定 ----------

SUBTITLE_TOP = 0.22   # 底部这一带的单行字当字幕，不算演职员表
MIN_BLOCK_FRAMES = 5  # 片尾块至少 5 帧（10 秒）有字
MAX_GAP_FRAMES = 3    # 块内允许连续 3 帧（6 秒）没认出字
END_SLACK_SEC = 90.0  # 片尾块结束后最多还剩 90 秒（台标、下集预告）
MIN_DENSITY = 0.6     # 块内有字帧占比
MIN_DARK = 0.7        # 帧里暗像素占比：演职员表多是黑底白字，叠在剧情画面上的不认


def _good_line(line: list) -> bool:
    text = line[0].strip()
    letters = sum(ch.isalpha() for ch in text)
    return len(text) >= 4 and letters >= 0.6 * len(text)


def _sentence(text: str) -> bool:
    """整句话（片尾字幕卡、尾声说明）：一行里有 4 个以上全小写的词。演职员表的职务名
    多是首字母大写或全大写，最多带 by / of / and 这类一两个小写词。"""
    return sum(1 for w in text.split() if w.isalpha() and w.islower()) >= 4


def _watermarks(frames: list[dict]) -> set:
    """同一位置同一串字出现在三成以上的帧里：台标/水印，判定时忽略。"""
    from collections import Counter
    seen = Counter()
    for f in frames:
        seen.update({_wm_key(ln) for ln in f["lines"]})
    limit = max(5, 0.3 * len(frames))
    return {k for k, c in seen.items() if c >= limit}


def _wm_key(line: list) -> tuple:
    return (line[0].strip().lower(), round(line[1], 1), round(line[2], 1))


def credit_frame(frame: dict, min_dark: float = MIN_DARK, ignore: set = frozenset()) -> bool:
    if frame["dark"] < min_dark:
        return False
    good = [ln for ln in frame["lines"] if _good_line(ln) and _wm_key(ln) not in ignore]
    if not good or any(_sentence(ln[0]) for ln in good):
        return False
    above = [ln for ln in good if ln[2] + ln[4] > SUBTITLE_TOP]
    # 演职员表至少两行（职务 + 名字）；只有底部的字是字幕，不算
    return len(good) >= 2 and (bool(above) or len(good) >= 3)


def decide(data: dict, min_dark: float = MIN_DARK) -> float | None:
    """返回片尾起点（秒），认不出返回 None。取「有字帧」最多的那一块的起点。"""
    frames = data["frames"]
    ignore = _watermarks(frames)
    flags = [credit_frame(f, min_dark, ignore) for f in frames]
    blocks = []  # [起始下标, 结束下标, 有字帧数]
    cur = None
    gap = 0
    for i, flag in enumerate(flags):
        if flag:
            if cur is None:
                cur = [i, i, 1]
            else:
                cur[1] = i
                cur[2] += 1
            gap = 0
        elif cur is not None:
            gap += 1
            if gap > MAX_GAP_FRAMES:
                blocks.append(cur)
                cur, gap = None, 0
    if cur is not None:
        blocks.append(cur)
    # 块要够长、块内有字帧够密，且结束在片尾 END_SLACK_SEC 以内（之后只允许台标/下集预告）
    end_limit = data["duration"] - END_SLACK_SEC
    blocks = [b for b in blocks if b[2] >= MIN_BLOCK_FRAMES
              and b[2] >= MIN_DENSITY * (b[1] - b[0] + 1)
              and frames[b[1]]["t"] >= end_limit]
    if not blocks:
        return None
    best = max(blocks, key=lambda b: (b[2], b[0]))
    if best[0] == 0 and data["start"] > 0:
        return None  # 片尾块从窗口第一帧就开始：真正起点可能更早，不下结论
    return max(0.0, frames[best[0]]["t"] - data["step"] / 2)
