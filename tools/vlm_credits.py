"""第二层：文字识别认不出片尾的集，让免费视觉模型看缩略图拼图判断剧情在哪一帧结束。

拼图：最后 VLM_TAIL_SEC 秒每 VLM_STEP_SEC 秒一帧，按顺序编号排成网格。
模型只回编号；取「第一张不再是剧情的帧」的时间当片尾起点——剧情在它前一帧之后才结束，
所以这个起点只会偏晚、不会切掉剧情。

模型走 AMD Token Factory（OpenAI 兼容接口，key 文件见下方 KEY_PATH）。拼图是公开剧集画面。
用法：
  .venv/bin/python tools/vlm_credits.py batch                   # 批量判，结果追加 vlm_results.jsonl，可续跑
  .venv/bin/python tools/vlm_credits.py eval <模型> <集id>...   # 打印模型判断
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from homecinema.detect import probe_duration  # noqa: E402

DB = os.path.expanduser("~/Library/Application Support/HomeCinema/library.db")
SHEETS = os.path.expanduser("~/Library/Caches/HomeCinema/vlm_sheets")
ENDPOINT = "https://developer.amd.com.cn/radeon/api/v1/chat/completions"
KEY_PATH = os.path.expanduser("~/.config/amd-tokenfactory.key")

VLM_TAIL_SEC = 300.0
VLM_STEP_SEC = 6.0
COLS = 8
THUMB_W = 240

PROMPT = """This is a contact sheet of the last 5 minutes of a TV episode, one frame every 6 seconds. \
Frames are numbered in time order (number in the top-left corner of each frame), left to right, top to bottom.

Find the FIRST frame where the episode's story is over. "Story over" means the frame shows only: \
end credits (names on a plain or static background), studio/network logos, a black screen, \
or a "next time"/"previously" preview montage. If credit text is overlaid on a scene that is still playing \
(characters acting or talking), that frame is still STORY.

Answer with JSON only: {"first_non_story": <frame number, or -1 if the story continues to the last frame>, \
"reason": "<a few words>"}"""


def build_sheet(path: str, duration: float, out_path: str) -> tuple[float, int]:
    """截帧拼图，返回 (第 0 帧的时间, 帧数)。"""
    from PIL import Image, ImageDraw, ImageFont

    start = max(0.0, duration - VLM_TAIL_SEC)
    with tempfile.TemporaryDirectory(prefix="hc-vlm-") as tmp:
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                        "-hwaccel", "videotoolbox", "-ss", f"{start:.3f}", "-i", path,
                        "-an", "-sn", "-vf", f"fps=1/{VLM_STEP_SEC},scale={THUMB_W}:-2",
                        "-q:v", "5", os.path.join(tmp, "f%04d.jpg")],
                       check=True, timeout=900, capture_output=True)
        names = sorted(n for n in os.listdir(tmp) if n.endswith(".jpg"))
        thumbs = [Image.open(os.path.join(tmp, n)).convert("RGB") for n in names]
    if not thumbs:
        raise RuntimeError("没截到帧")
    w, h = thumbs[0].size
    rows = (len(thumbs) + COLS - 1) // COLS
    sheet = Image.new("RGB", (COLS * w, rows * h), (40, 40, 40))
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 22)
    except OSError:
        font = ImageFont.load_default()
    for i, im in enumerate(thumbs):
        x, y = (i % COLS) * w, (i // COLS) * h
        sheet.paste(im.resize((w, h)), (x, y))
        d = ImageDraw.Draw(sheet)
        d.rectangle([x, y, x + 34, y + 26], fill=(0, 0, 0))
        d.text((x + 3, y + 1), str(i), fill=(255, 220, 0), font=font)
    sheet.save(out_path, quality=82)
    # fps 滤镜第 i 帧在 start + (i + 0.5) * step 附近
    return start + 0.5 * VLM_STEP_SEC, len(thumbs)


def ask(model: str, image_path: str, retries: int = 3) -> dict:
    key = open(KEY_PATH).read().strip()
    with open(image_path, "rb") as fh:
        b64 = base64.b64encode(fh.read()).decode()
    # 关思考：开着时每次推理 2500+ token、76 秒；关掉 5 秒，抽查答案相同
    body = {"model": model, "temperature": 0, "max_tokens": 800,
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": PROMPT},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}]}]}
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(ENDPOINT, data=json.dumps(body).encode(),
                                         headers={"Authorization": f"Bearer {key}",
                                                  "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=180) as resp:
                data = json.loads(resp.read())
            text = data["choices"][0]["message"].get("content") or ""
            m = re.search(r"\{[^{}]*\"first_non_story\"[^{}]*\}", text, re.S)
            if not m:
                raise ValueError(f"回复里没有 JSON：{text[-200:]!r} finish={data['choices'][0].get('finish_reason')}")
            out = json.loads(m.group(0))
            out["usage"] = data.get("usage")
            return out
        except Exception as exc:  # 网络/限速/格式问题退避重试
            last = exc
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"{model} 调用失败：{last}")


def sheet_for(eid: int) -> tuple[str, float, int, float]:
    os.makedirs(SHEETS, exist_ok=True)
    con = sqlite3.connect(DB)
    path = con.execute("SELECT path FROM episodes WHERE id=?", (eid,)).fetchone()[0]
    dur = probe_duration(path)
    out = os.path.join(SHEETS, f"{eid}.jpg")
    meta = out + ".json"
    if os.path.exists(out) and os.path.exists(meta):
        m = json.load(open(meta))
        return out, m["t0"], m["n"], dur
    t0, n = build_sheet(path, dur, out)
    json.dump({"t0": t0, "n": n}, open(meta, "w"))
    return out, t0, n, dur


def credits_from_answer(ans: dict, t0: float, n: int) -> float | None:
    k = int(ans.get("first_non_story", -1))
    if k < 0 or k >= n:
        return None
    if k == 0:
        return None  # 拼图第一帧就不是剧情：看的窗口不够早，不下结论
    return t0 + k * VLM_STEP_SEC


RESULTS = os.path.expanduser("~/Library/Caches/HomeCinema/vlm_results.jsonl")
MODEL = "Qwen3.8-Flash-Next"  # 12 集对照：与文字识别结果差 6 秒内；Qwen3.8-27B 两集把片尾蒙太奇判成剧情


def batch_targets() -> list[int]:
    """文字识别认不出片尾、且原来没有片尾或只有声纹片尾的集。"""
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from homecinema import ocr_credits as oc
    cache = os.path.expanduser("~/Library/Caches/HomeCinema/ocr_tail")
    con = sqlite3.connect(DB)
    out = []
    for eid, c0, source in con.execute(
            "SELECT e.id, s.credits_start, s.source FROM episodes e "
            "JOIN segments s ON s.episode_id = e.id WHERE e.missing = 0 ORDER BY e.id"):
        f = os.path.join(cache, f"{eid}.json")
        if not os.path.exists(f):
            continue
        if oc.decide(json.load(open(f, encoding="utf-8"))) is not None:
            continue
        if c0 is None or source == "fingerprint":
            out.append(eid)
    return out


def run_batch() -> int:
    done = set()
    if os.path.exists(RESULTS):
        done = {json.loads(l)["eid"] for l in open(RESULTS, encoding="utf-8")}
    todo = [e for e in batch_targets() if e not in done]
    print(f"视觉模型待判 {len(todo)} 集（已判 {len(done)}）", flush=True)
    for i, eid in enumerate(todo, 1):
        t = time.time()
        try:
            img, t0, n, dur = sheet_for(eid)
            ans = ask(MODEL, img)
            rec = {"eid": eid, "credits": credits_from_answer(ans, t0, n),
                   "k": ans.get("first_non_story"), "n": n, "reason": ans.get("reason")}
        except Exception as exc:  # 失败不写结果，重跑时再判
            print(f"[{i}/{len(todo)}] {eid} 失败 {exc}"[:300], flush=True)
            continue
        with open(RESULTS, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(f"[{i}/{len(todo)}] {eid} k={rec['k']} {time.time() - t:.1f}s {rec['reason']}", flush=True)
    print("视觉模型判定完成", flush=True)
    return 0


def main() -> int:
    if len(sys.argv) >= 2 and sys.argv[1] == "batch":
        return run_batch()
    if len(sys.argv) >= 4 and sys.argv[1] == "eval":
        model = sys.argv[2]
        for eid in map(int, sys.argv[3:]):
            img, t0, n, dur = sheet_for(eid)
            t = time.time()
            try:
                ans = ask(model, img)
                print(json.dumps({"eid": eid, "credits": credits_from_answer(ans, t0, n),
                                  "k": ans.get("first_non_story"), "reason": ans.get("reason"),
                                  "sec": round(time.time() - t, 1), "usage": ans.get("usage")},
                                 ensure_ascii=False), flush=True)
            except Exception as exc:
                print(json.dumps({"eid": eid, "error": str(exc)[:300]}, ensure_ascii=False), flush=True)
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
