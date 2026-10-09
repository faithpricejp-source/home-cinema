"""剧集片头/片尾自动识别算法。

两条路径：
1. 视频带章节（标题写着 Intro / Credits 等）时用 `chapters_to_segments()` 直接取。
2. 没有章节时用声纹：对每集开头/结尾若干分钟算 Chromaprint 声纹（`fingerprint()`），
   在同一季各集之间找共同片段（`find_shared()` / `detect_season()`）。

只依赖 Python 标准库，外部命令用 ffmpeg / fpcalc。
"""

from __future__ import annotations

import math
import os
import re
import shutil
import subprocess
import tempfile

# 实测值：44100Hz 单声道下 fpcalc -raw 每个整数覆盖的秒数。
# 用多个长度（50..400s）线性拟合 整数个数 = 8.0771 * 秒数 - 21.6，
# 斜率的倒数 = 0.123806s，即 SAMPLE_SEC。
SAMPLE_SEC = 0.1238

# max_bit_diff 默认 8（Intro Skipper 用 6）：真实片库里同一首片头曲、不同集的压制版本，
# 对齐后平均每个整数差约 5 位，只有约 70% 落在 6 位以内，会被断点比例门槛整段否掉
# （2026-10-02 用一部美剧同季两集实测）。8 位时召回回来、片头长度仍准；
# 不相干音频之间实测仍然配不上。

# 共同片段里「断点」总时长占整段的比例上限，见 find_shared()
MAX_GAP_RATIO = 0.25

# 修剪候选片段两头虚接边缘用的局部窗口宽度（秒）与窗口内最低匹配率
TRIM_WINDOW_SEC = 3.0
TRIM_MIN_RATE = 0.6

_FFMPEG = "ffmpeg"
_FPCALC = "fpcalc"
_PROBE = "ffprobe"


def _tool(name: str) -> str:
    """找到外部命令，优先 PATH，其次 Homebrew 目录。"""
    found = shutil.which(name)
    if found:
        return found
    guess = os.path.join("/opt/homebrew/bin", name)
    return guess if os.path.exists(guess) else name


def _run(cmd: list[str], timeout: float = 300.0) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def fingerprint(path: str, start: float, length: float) -> list[int]:
    """取视频 path 从 start 秒起 length 秒的音频声纹（fpcalc -raw 输出的整数列表）。

    实现：ffmpeg -ss start -t length 解出 44100Hz 单声道 wav 到临时文件，
    再用 fpcalc -raw 读取，解析 FINGERPRINT= 行的逗号分隔整数。
    文件不存在、没有音轨、解码失败或声纹为空都返回 []。
    返回值第 i 个整数对应音频时间 start + i * SAMPLE_SEC 附近。
    """
    if length <= 0 or not os.path.exists(path):
        return []
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        decode = _run(
            [
                _tool(_FFMPEG), "-hide_banner", "-loglevel", "error", "-y",
                "-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", path,
                "-map", "0:a:0", "-ac", "1", "-ar", "44100", "-f", "wav", tmp_path,
            ]
        )
        if decode.returncode != 0:
            return []
        calc = _run(
            [
                _tool(_FPCALC), "-raw",
                "-length", f"{math.ceil(length) + 1}",
                tmp_path,
            ]
        )
        if calc.returncode != 0:
            return []
        return _parse_fpcalc(calc.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def _parse_fpcalc(text: str) -> list[int]:
    """解析 fpcalc 输出里 FINGERPRINT= 行（可能多行）为整数列表。"""
    values: list[int] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("FINGERPRINT="):
            continue
        body = line[len("FINGERPRINT="):].strip()
        if not body:
            continue
        for token in body.split(","):
            token = token.strip()
            if not token:
                continue
            try:
                values.append(int(token))
            except ValueError:
                continue
    return values


def _bits_diff(x: int, y: int) -> int:
    """两个 32 位声纹整数不同的位数。"""
    return ((x ^ y) & 0xFFFFFFFF).bit_count()


STRICT_BIT_DIFF = 6


def find_shared(
    a: list[int],
    b: list[int],
    *,
    max_bit_diff: int = 8,
    min_len_sec: float = 15.0,
    max_gap_sec: float = 3.5,
) -> tuple[float, float, float, float] | None:
    """先按 6 位的严门槛找（边界最准）；找不到再按 max_bit_diff（默认 8）找（召回）。
    宽门槛会把片段两头各多延伸一两秒，所以只在严门槛找不到时才用。参数含义见 _find_shared_at。"""
    if max_bit_diff > STRICT_BIT_DIFF:
        strict = _find_shared_at(a, b, max_bit_diff=STRICT_BIT_DIFF,
                                 min_len_sec=min_len_sec, max_gap_sec=max_gap_sec)
        if strict is not None:
            return strict
    return _find_shared_at(a, b, max_bit_diff=max_bit_diff,
                           min_len_sec=min_len_sec, max_gap_sec=max_gap_sec)


def _find_shared_at(
    a: list[int],
    b: list[int],
    *,
    max_bit_diff: int = 8,
    min_len_sec: float = 15.0,
    max_gap_sec: float = 3.5,
) -> tuple[float, float, float, float] | None:
    """在两段声纹里找最长的「共同片段」。

    返回 (a_start_sec, a_end_sec, b_start_sec, b_end_sec)，秒数相对各自声纹开头；
    找不到（最长共同片段短于 min_len_sec）返回 None。

    两个整数「相同」指 popcount(x ^ y) <= max_bit_diff。
    遍历 a 相对 b 的全部相对偏移（d = a_index - b_index，范围 1-len(b) .. len(a)-1），
    每个偏移下沿对角线扫描连续的相同段：单个断点不超过 max_gap_sec，
    且整段里断点总时长不超过 MAX_GAP_RATIO（否则两段毫不相干的声纹会因为
    一路「偶发相似」被连成一段）。再用 _trim_edges() 按局部匹配率掐掉两头虚接的边缘
    （否则共同片段会往两边各拖出好几秒），剩下的长度还要 >= min_len_sec，取最长的那一段。
    """
    la, lb = len(a), len(b)
    if la == 0 or lb == 0:
        return None

    min_ticks = max(1, math.ceil(min_len_sec / SAMPLE_SEC))
    gap_ticks = max(0, int(max_gap_sec / SAMPLE_SEC))
    if min_ticks > min(la, lb):
        return None

    best_len = 0
    best_a0 = best_a1 = best_d = 0

    for d in _candidate_offsets(a, b, max_bit_diff, min_ticks):
        # 对角线上 a 的下标区间 [i0, i1)，对应的 b 下标是 i - d
        i0 = max(0, d)
        i1 = min(la, lb + d)
        if i1 - i0 < min_ticks:
            continue
        i = i0
        while i < i1:
            if _bits_diff(a[i], b[i - d]) > max_bit_diff:
                i += 1
                continue
            seg_start = i
            seg_last = i
            matched = 1
            i += 1
            gap = 0
            while i < i1:
                if _bits_diff(a[i], b[i - d]) <= max_bit_diff:
                    seg_last = i
                    matched += 1
                    gap = 0
                else:
                    gap += 1
                    if gap > gap_ticks:
                        break
                    i += 1
                    continue
                i += 1
            span = seg_last - seg_start + 1
            if span - matched > MAX_GAP_RATIO * span:
                # 断点太多，不算共同片段；i 已经停在断点之后，继续找下一段
                continue
            flags = [
                1 if _bits_diff(a[k], b[k - d]) <= max_bit_diff else 0
                for k in range(seg_start, seg_last + 1)
            ]
            edges = _trim_edges(flags)
            if edges is None:
                continue
            s, e = seg_start + edges[0], seg_start + edges[1]
            if e - s >= min_ticks and e - s > best_len:
                best_len = e - s
                best_a0 = s
                best_a1 = e
                best_d = d
            i = seg_last + 1

    if best_len < min_ticks:
        return None
    b0 = best_a0 - best_d
    b1 = best_a1 - best_d
    return (
        best_a0 * SAMPLE_SEC,
        best_a1 * SAMPLE_SEC,
        b0 * SAMPLE_SEC,
        b1 * SAMPLE_SEC,
    )


# 粗筛：每个错位先用 numpy 数「相同」的点，只对点数最多的前 TOP_OFFSETS 个错位做精细扫描。
# 全量扫描是 O(len(a)·len(b)) 的纯 Python 循环，600 秒窗口一对要几秒，整个片库要跑一天；
# 共同片段所在的错位，相同点数一定远高于其他错位，前 12 个足够把它包含进去。
TOP_OFFSETS = 12


def _candidate_offsets(a: list[int], b: list[int], max_bit_diff: int, min_ticks: int):
    la, lb = len(a), len(b)
    try:
        import numpy as np
    except ImportError:  # 没装 numpy 就退回全量扫描
        return range(1 - lb, la)
    A = np.asarray(a, dtype=np.int64).astype(np.uint32)
    B = np.asarray(b, dtype=np.int64).astype(np.uint32)
    scored = []
    for d in range(1 - lb, la):
        i0, i1 = max(0, d), min(la, lb + d)
        if i1 - i0 < min_ticks:
            continue
        hits = int(np.count_nonzero(np.bitwise_count(A[i0:i1] ^ B[i0 - d:i1 - d]) <= max_bit_diff))
        if hits >= min_ticks * (1 - MAX_GAP_RATIO):
            scored.append((hits, d))
    scored.sort(reverse=True)
    return [d for _, d in scored[:TOP_OFFSETS]]


def _trim_edges(flags: list[int]) -> tuple[int, int] | None:
    """按局部匹配率掐掉候选片段两头「虚接」的部分。

    flags 是候选片段逐整数的匹配标记（1 = 相同）。从头/从尾各找一个
    TRIM_WINDOW_SEC 宽的窗口，要求窗口内匹配率 >= TRIM_MIN_RATE，
    两个窗口之间就是可信的共同片段；整段都找不到合格窗口返回 None。
    """
    n = len(flags)
    width = max(2, int(round(TRIM_WINDOW_SEC / SAMPLE_SEC)))
    if n <= width:
        return (0, n) if sum(flags) >= TRIM_MIN_RATE * n else None
    prefix = [0] * (n + 1)
    for idx, flag in enumerate(flags):
        prefix[idx + 1] = prefix[idx] + flag
    need = TRIM_MIN_RATE * width
    left = None
    for start in range(0, n - width + 1):
        if prefix[start + width] - prefix[start] >= need:
            left = start
            break
    right = None
    for end in range(n, width - 1, -1):
        if prefix[end] - prefix[end - width] >= need:
            right = end
            break
    if left is None or right is None:
        return None
    return (left, max(right, left + width))


NEIGHBORS = 3


def detect_season(
    fps: dict[str, list[int]],
    *,
    offset_sec: float = 0.0,
    min_len_sec: float = 15.0,
    max_len_sec: float = 150.0,
) -> dict[str, tuple[float, float] | None]:
    """一季的各集声纹 {集的键: 声纹} → {集的键: (start_sec, end_sec) 或 None}。

    秒数相对每集声纹的开头，返回值再加 offset_sec（片尾检测时声纹是从
    「片长 - N 秒」开始取的，调用方把这个起点传进来）。

    做法：每集跟同季其他集两两 find_shared，得到该集视角下的候选片段；
    丢掉长度不在 [min_len_sec, max_len_sec] 的候选；剩下的按「与其他候选重叠
    过半」互相计票，取票数最高的候选（并列取起点最早的）。
    一集至少要跟另一集找到共同片段才有结果，否则 None。只有 1 集时全部 None。
    """
    keys = list(fps.keys())
    result: dict[str, tuple[float, float] | None] = {k: None for k in keys}
    if len(keys) < 2:
        return result

    candidates: dict[str, list[tuple[float, float]]] = {k: [] for k in keys}
    # 每集只跟后面 NEIGHBORS 集比（前面的已经比过），每集最多 2×NEIGHBORS 个比对对象：
    # 全两两比对是 O(n²)，整个片库 3.8 万对；邻近几集足够投票，也更贴近同一阶段的片头
    for x in range(len(keys)):
        for y in range(x + 1, min(len(keys), x + 1 + NEIGHBORS)):
            ka, kb = keys[x], keys[y]
            shared = find_shared(
                fps[ka], fps[kb], min_len_sec=min_len_sec
            )
            if shared is None:
                continue
            for key, s, e in ((ka, shared[0], shared[1]), (kb, shared[2], shared[3])):
                if min_len_sec <= e - s <= max_len_sec:
                    candidates[key].append((s, e))

    for key in keys:
        picked = _vote(candidates[key])
        if picked is None:
            continue
        result[key] = (picked[0] + offset_sec, picked[1] + offset_sec)
    return result


def _vote(items: list[tuple[float, float]]) -> tuple[float, float] | None:
    """在候选片段里选出「多数集都认可」的那一段。

    先按互相重叠（重叠长度 >= 两者较短长度的 50%）计票，只留票数最高的那一簇；
    一簇里有多个候选时，取离本簇起点中位数 + 终点中位数最近的那个——
    各对配对给出的边界会有一两秒出入，中位数比「最长」更稳。
    """
    if not items:
        return None

    def overlaps(x: tuple[float, float], y: tuple[float, float]) -> bool:
        shared = min(x[1], y[1]) - max(x[0], y[0])
        shorter = min(x[1] - x[0], y[1] - y[0])
        return shorter > 0 and shared >= 0.5 * shorter

    votes = [sum(1 for other in items if overlaps(c, other)) for c in items]
    top = max(votes)
    cluster = [c for c, v in zip(items, votes) if v == top]
    # 票数并列的可能是互不重叠的两簇：只留与起点最早那个重叠的一簇（并列取起点最早），
    # 中位数只在簇内平滑边界抖动，不能把两段不相干的区间平均成一个不存在的位置
    first = min(cluster, key=lambda c: c[0])
    cluster = [c for c in cluster if overlaps(c, first)]
    if len(cluster) == 1:
        return cluster[0]
    starts = sorted(c[0] for c in cluster)
    ends = sorted(c[1] for c in cluster)
    mid = len(cluster) // 2
    med_start = starts[mid] if len(cluster) % 2 else (starts[mid - 1] + starts[mid]) / 2
    med_end = ends[mid] if len(cluster) % 2 else (ends[mid - 1] + ends[mid]) / 2
    return min(cluster, key=lambda c: (abs(c[0] - med_start) + abs(c[1] - med_end)))


_INTRO_TITLES = (
    "intro", "opening", "op", "title sequence", "opening credits",
    "片头", "オープニング",
)
_CREDITS_TITLES = (
    "credits", "ending", "ed", "end credits", "outro",
    "片尾", "エンディング",
)
# "Chapter 1" / "第 01 章" / "Scene 2" / "第3集" 这类纯编号标题
_NUMBER_TITLE_RE = re.compile(
    r"^\s*(?:第\s*)?(?:chapter|scene|track|part|seg(?:ment)?|cap[ií]tulo|"
    r"章|节|集|幕|段|话)?\s*[-_#]*\s*"
    r"[0-9〇零一二三四五六七八九十百]+\s*"
    r"(?:章|节|集|幕|段|话)?\s*$",
    re.IGNORECASE,
)


def _title_hit(title: str, words: tuple[str, ...]) -> bool:
    """标题是否整词/整串匹配给定短语之一（不分大小写）。"""
    t = title.strip().lower()
    if not t:
        return False
    if t in words:
        return True
    t = re.sub(r"[\s\-_.:：·]+$", "", t)
    t = re.sub(r"^[\s\-_.:：·]+", "", t)
    if t in words:
        return True
    for word in words:
        if not word.isascii():
            # 片头/片尾/オープニング 这类非 ASCII 词没有「整词」概念，按子串匹配
            if word in t:
                return True
        elif len(word) <= 2:
            # op / ed 这类缩写只认整串标题，否则 "no player" 会命中 "op"
            if re.fullmatch(word, t):
                return True
        elif re.search(r"(?<![a-z0-9])" + re.escape(word) + r"(?![a-z0-9])", t):
            return True
    return False


def _as_sec(value) -> float | None:
    """ffprobe 的时间字段（秒或 HH:MM:SS.frac）转成秒。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        if ":" in text:
            parts = text.split(":")
            sec = float(parts[-1])
            minute = float(parts[-2]) if len(parts) >= 2 else 0.0
            hour = float(parts[-3]) if len(parts) >= 3 else 0.0
            return hour * 3600 + minute * 60 + sec
        return float(text)
    except ValueError:
        return None


def chapters_to_segments(chapters: list[dict], duration: float) -> dict:
    """ffprobe -show_chapters -of json 的 chapters 列表 → 片头/片尾区间。

    chapters 每项形如 {"start_time": "12.000000", "end_time": "72.000000",
    "tags": {"title": "Intro"}}；时间也接受 HH:MM:SS.frac。

    返回 {"intro": (s, e) 或 None, "credits": (s, e) 或 None}。
    片头：标题匹配 intro/opening/op/title sequence/opening credits/片头/オープニング，
    且整段落在片子前 1/3，长度 10–180 秒。
    片尾：标题匹配 credits/ending/ed/end credits/outro/片尾/エンディング，
    且起点在片子后 1/3。
    编号型标题（"Chapter 1"、"第 01 章"、"Scene 2"）不算。
    """
    out = {"intro": None, "credits": None}
    duration = float(duration or 0.0)
    if duration <= 0:
        return out
    third = duration / 3.0

    for chapter in chapters or []:
        if not isinstance(chapter, dict):
            continue
        tags = chapter.get("tags") or {}
        title = str(tags.get("title") or chapter.get("title") or "")
        start = _as_sec(chapter.get("start_time"))
        end = _as_sec(chapter.get("end_time"))
        if start is None or end is None or end <= start:
            continue
        if _NUMBER_TITLE_RE.search(title.strip()):
            continue
        if _title_hit(title, _INTRO_TITLES):
            # 整段落在前 1/3，长度 10-180 秒
            if start >= 0.0 and end <= third and 10.0 <= end - start <= 180.0:
                if out["intro"] is None:
                    out["intro"] = (start, end)
        elif _title_hit(title, _CREDITS_TITLES):
            # 起点落在后 1/3；章节结尾偶尔会超出总时长一点，夹到总时长
            if start >= 2.0 * third and start < duration and end - start >= 5.0:
                if out["credits"] is None:
                    out["credits"] = (start, min(end, duration))
    return out
