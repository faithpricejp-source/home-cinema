"""文字识别片尾判定（ocr_credits.decide）：只测判定规则，帧特征手工构造。"""

from homecinema import ocr_credits as oc


def frame(t, dark=0.95, lines=()):
    return {"t": t, "dark": dark, "lines": [list(x) for x in lines]}


CREDIT = (("Executive Producer", 0.4, 0.55, 0.2, 0.03), ("JOHN SMITH", 0.4, 0.47, 0.2, 0.05))
SUB = (("I can't just walk up to him", 0.3, 0.02, 0.4, 0.03),)


def episode(frames, duration=1400.0, start=1100.0):
    return {"duration": duration, "start": start, "step": 2.0, "frames": frames}


def story(t0, t1):
    return [frame(t, dark=0.3, lines=SUB) for t in range(t0, t1, 2)]


def credits(t0, t1):
    # 每帧名字不同（同一串字出现在三成以上的帧里会被当成台标）
    return [frame(t, lines=((f"Producer Role{chr(65 + t % 26)}", 0.4, 0.55, 0.2, 0.03),
                            (f"PERSON NAME{chr(65 + t % 26)}{chr(65 + t // 26 % 26)}", 0.4, 0.47, 0.2, 0.05)))
            for t in range(t0, t1, 2)]


def test_credits_block_start():
    ep = episode(story(1101, 1301) + credits(1301, 1399))
    assert oc.decide(ep) == 1300.0


def test_subtitles_only_is_none():
    ep = episode([frame(t, dark=0.9, lines=SUB) for t in range(1101, 1399, 2)])
    assert oc.decide(ep) is None


def test_bright_overlay_not_credits():
    # 演职员表叠在明亮的剧情画面上：不认（宁可不跳）
    ep = episode(story(1101, 1301) + [frame(t, dark=0.2, lines=CREDIT) for t in range(1301, 1399, 2)])
    assert oc.decide(ep) is None


def test_watermark_ignored():
    logo = ("Channel", 0.9, 0.9, 0.05, 0.03)
    wm_story = [frame(t, dark=0.8, lines=(logo, ("Where are you going", 0.5, 0.5, 0.2, 0.03)))
                for t in range(1101, 1301, 2)]
    # 剧情帧里只有台标 + 一行字：台标被忽略后不足两行，不算片尾
    ep = episode(wm_story + credits(1301, 1399))
    assert oc.decide(ep) == 1300.0


def test_epilogue_sentence_card_skipped():
    card = (("The operation that failed was not to succeed", 0.3, 0.6, 0.4, 0.03),
            ("and the war would go on for another year", 0.3, 0.55, 0.4, 0.03))
    ep = episode(story(1101, 1281) + [frame(t, lines=card) for t in range(1281, 1301, 2)]
                 + credits(1301, 1399))
    assert oc.decide(ep) == 1300.0


def test_block_far_from_end_rejected():
    # 片中一段黑底文字（结束离片尾 > 90 秒），之后剧情继续到结尾
    ep = episode(story(1101, 1151) + credits(1151, 1181) + story(1181, 1399))
    assert oc.decide(ep) is None


def test_block_touching_window_start_is_none():
    ep = episode(credits(1101, 1399))
    assert oc.decide(ep) is None


def test_dark_scene_dialog_not_credits():
    # 暗场对白（双语字幕，识别成三行、位置偏上）紧挨片尾：问号/省略号/叹号结尾的不算演职员表
    dialog = [(("Someone from Pasadena,", 0.3, 0.2, 0.4, 0.04), ("California named...", 0.3, 0.16, 0.4, 0.04),
               ("Someone from Pasadena, California named...", 0.3, 0.02, 0.4, 0.03)),
              (("Who's doing that?", 0.3, 0.2, 0.4, 0.04), ("Who's doing that?", 0.3, 0.02, 0.4, 0.03),
               ("Who is that", 0.3, 0.16, 0.4, 0.04))]
    ep = episode(story(1101, 1281) + [frame(t, dark=0.8, lines=dialog[t // 2 % 2]) for t in range(1281, 1301, 2)]
                 + credits(1301, 1399))
    assert oc.decide(ep) == 1300.0
