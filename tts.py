# -*- coding: utf-8 -*-
"""
tts.py —— 模板 + 预生成音频拼接播放（运行时完全不调 TTS）
作用：回复句式是有限的，所以固定句式直接播**预先用奶龙音色生成好的整段 wav**，
      只有槽位（课程名、地点、时间）需要临时拼。
主要模块：TEMPLATES（模板表）、render()、speak()、speech_text()、human_time()。
相关文件：音频素材在 config.VOICE_DIR（用 gen_voice.py 生成）；播放走 audio.py。

拼接规则：**文件名就是它读的文字**，speak() 做「最长匹配优先」。
   有 第一节是.wav + 高等数学.wav      → 按片段播，语气自然
   只有 第.wav 一.wav 节.wav …        → 退化成逐字，能听懂但机械
所以素材粒度越粗越好；缺哪个补哪个，不用改代码。

注意：文件名和运行时文本都要过 speech_text()（只留汉字、数字转中文），
两边的规整函数必须是同一个，否则永远匹配不上。
"""
import re
import wave
from datetime import date, datetime

import numpy as np

import config
from audio import play

# 分句用的标点：只影响停顿，不参与匹配
PUNCT = "，。！？；：、,.!?;:…"

TEMPLATES = {
    "ack":            "我在。",              # 唤醒应答，像小爱那样先应一声
    "none":           "没有{what}。",
    # 数量句按"问的是哪一类"选，明细句按"最前面那条是什么"选。
    # 拆开是因为问"这周有什么"时候两类混在一起，说"六个作业没交"就是错的。
    "count.deadline": "你有{count}个作业没交。",
    "count.schedule": "有{count}个安排。",
    "count.any":      "有{count}个事项。",
    "urgent":         "最急的是{title}，{remaining}。",
    "nearest":        "最近的是{title}，{when}。",
    "nearest.loc":    "最近的是{title}，{when}，在{location}。",
    "stale":          "课表已经{age}没更新了，可能不是最新的。",
    "write.wip":      "改数据还没接通，先去电脑上改吧。",
    "fail":           "没办成，{reason}。",
    "unknown":        "这个我还不太会，你换个说法试试。",
    "not_heard":      "没听清，请再说一次。",
    "net":            "连不上电脑那边，等会儿再试试。",
    "auth":           "电脑那边说密钥不对，检查一下配置。",
    "config":         "还没配置模型密钥，先把配置填好。",
    "llm":            "模型那边没响应，等会儿再问我一次。",
}

_MAX_PHRASE = 40          # 最长匹配字数：要能容下"习近平新时代中国特色社会主义思想概论"这种长课名
_CACHE = {}

_D = "零一二三四五六七八九"


# ---------------- 数字与时间的中文读法 ----------------

def cn_int(n, liang=False):
    """0-9999 的中文读法：15->十五，105->一百零五。

    liang=True 时 2 读作「两」——中文量词前要说「两节课」「下午两点」，
    说「二节课」「二点」就不像人话了。
    """
    if n == 0:
        return "零"
    if n == 2 and liang:
        return "两"
    if n >= 10000:
        return "".join(_D[int(c)] for c in str(n))
    out, zero = "", False
    for unit, val in (("千", 1000), ("百", 100), ("十", 10), ("", 1)):
        d = n // val % 10
        if d:
            out += ("零" if zero else "") + _D[d] + unit
            zero = False
        elif out and val > 1:
            zero = True
    return out[1:] if out.startswith("一十") else out


def cn_text(text):
    """阿拉伯数字转中文读法。

    3 位以上按位念（年份 2026→二零二六、教室号 415→四一五），
    1~2 位按数值念（10→十、26→二十六）。
    """
    def one(m):
        s = m.group()
        return "".join(_D[int(c)] for c in s) if len(s) >= 3 else cn_int(int(s))
    return re.sub(r"\d+", one, text)


def _flatten(text):
    """把要念的文本压成「可查素材」的形式。

    返回 (flat, pause)：flat 只含汉字；pause[k] 表示第 k 个字后面要不要停顿。
    英文、括号、加号、连字符这些念不出来也查不到素材的字符直接丢掉——
    "程序设计思想与方法（C++）"会念成"程序设计思想与方法"，正是人话。
    """
    flat, pause = [], []
    for ch in cn_text(text):
        if ch in PUNCT or ch.isspace():
            if pause:
                pause[-1] = True
            continue
        if not ("\u4e00" <= ch <= "\u9fff"):
            continue
        flat.append(ch)
        pause.append(False)
    return "".join(flat), pause


def speech_text(text):
    """素材命名用：和运行时匹配键完全一致。"""
    return _flatten(text)[0]


def _parse_dt(s):
    """接口时间 '2026-10-22 08:00' 或 ISO 字符串 → datetime；解析不了返回 None。"""
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except (TypeError, ValueError):
            pass
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def human_time(iso):
    """接口时间说成人话：'2026-10-22 08:00' → 今天上午八点。"""
    t = _parse_dt(iso)
    if t is None:
        return iso or ""

    delta = (t.date() - date.today()).days
    day = {0: "今天", 1: "明天", 2: "后天", -1: "昨天"}.get(delta)
    if day is None:
        day = f"周{'一二三四五六日'[t.weekday()]}" if 0 < delta < 7 else \
              f"{cn_int(t.month)}月{cn_int(t.day)}日"
    h, m = t.hour, t.minute
    if h < 6:
        ap, h12 = "凌晨", h
    elif h < 12:
        ap, h12 = "上午", h
    elif h == 12:
        ap, h12 = "中午", 12
    elif h < 18:
        ap, h12 = "下午", h - 12
    else:
        ap, h12 = "晚上", h - 12
    out = f"{day}{ap}{cn_int(h12, liang=True)}点"
    if m:
        out += f"{cn_int(m, liang=True)}分"
    return out


# ---------------- 模板与拼接 ----------------

def render(template, **slots):
    """{title} 这类槽位由调用方填，值应当是已经口语化的中文。"""
    return template.format(**slots)


def _trim(a):
    """去掉首尾静音，两边各留 12ms 余量。

    SAPI 这类引擎写出的每个片段前后都带近半秒空白（实测单字 wav 就有 1.06 秒），
    逐字拼起来会一顿一顿的。切在加载处，对所有引擎都有效，奶龙素材同样受益。
    """
    loud = np.flatnonzero(np.abs(a) > 300)          # int16 尺度，约 1% 满量程
    if loud.size == 0:
        return a
    margin = int(0.012 * config.SAMPLE_RATE)
    return a[max(0, loud[0] - margin): loud[-1] + margin]


def _load(name):
    """按文件名（= 它读的文字）取 wav；不存在返回 None，格式不对直接报错。"""
    if name not in _CACHE:
        _CACHE[name] = None
        p = config.VOICE_DIR / f"{name}.wav"
        if not p.exists():
            return None
        with wave.open(str(p), "rb") as w:
            if w.getframerate() != config.SAMPLE_RATE or w.getnchannels() != 1:
                raise SystemExit(
                    f"[tts] {p.name} 格式不对：需要 16bit/16kHz/单声道，"
                    f"实际 {w.getframerate()}Hz/{w.getnchannels()}声道（重跑 gen_voice.py）")
            _CACHE[name] = _trim(
                np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16))
    return _CACHE[name]


def has_ack_clip():
    """有没有角色原声的应答片段（resources/voice/_ack.wav）。"""
    return (config.VOICE_DIR / "_ack.wav").exists()


def play_ack():
    """播唤醒应答。返回 True 表示播了角色原声。

    原声片段不参与「文件名=文字」那套拼接，它是一整段录音，
    所以单独用 _ack.wav 放：把角色自己说的那句"我是奶龙"截下来当应答，
    比合成的"我在"有辨识度得多。
    """
    clip = _load("_ack")
    if clip is None:
        return False
    play(clip)
    return True


def _match(text, i):
    """从位置 i 开始找最长的有素材片段，返回 (音频, 字长)。"""
    for n in range(min(_MAX_PHRASE, len(text) - i), 0, -1):
        clip = _load(text[i:i + n])
        if clip is not None:
            return clip, n
    return None, 0


def _system_say(text):
    """临时兜底：没有奶龙素材时用系统自带 TTS 念，先把对话链路验证通。

    Windows 走 System.Speech（要装中文语音包，Win10/11 一般自带）；
    树莓派走 espeak-ng（音质很差，只为听个响）。
    """
    import shutil
    import subprocess
    import sys

    if sys.platform == "win32":
        ps = ("Add-Type -AssemblyName System.Speech; "
              f"(New-Object System.Speech.Synthesis.SpeechSynthesizer).Speak('{text.replace(chr(39), chr(39) * 2)}')")
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=False)
        return
    exe = shutil.which("espeak-ng") or shutil.which("espeak")
    if not exe:
        print("[tts] 没找到 espeak-ng，先装：sudo apt install espeak-ng")
        return
    subprocess.run([exe, "-v", "zh", text], check=False)


def speak(text):
    """拼出音频并播放；缺素材的片段跳过并汇总提示。"""
    if config.SYSTEM_TTS:            # 临时兜底，正式素材到位后不用它
        print("[tts] 系统语音（临时）")
        return _system_say(text)

    flat, pause = _flatten(text)
    if not flat:
        return
    gap = np.zeros(int(config.GAP_SEC * config.SAMPLE_RATE), dtype=np.int16)
    clips, missing, i = [], [], 0
    while i < len(flat):
        clip, n = _match(flat, i)
        if clip is None:
            missing.append(flat[i])
            i += 1
            continue
        clips += [clip, gap]
        if any(pause[i:i + n]):      # 片段内部跨了标点，额外补一个停顿
            clips.append(gap)
        i += n
    if not clips:
        print("[tts] 还没有语音素材，先跑 python gen_voice.py。本该说：", flat)
        return
    play(np.concatenate(clips[:-1]))
    if missing:
        print(f"[tts] 缺素材 {len(missing)} 处（已跳过）：{''.join(missing)}")
        _note_missing(missing)


def _note_missing(missing):
    """把缺的字记到 _missing.txt，下次跑 gen_voice.py 会自动补上。"""
    try:
        p = config.VOICE_DIR / "_missing.txt"
        old = p.read_text("utf-8").split() if p.exists() else []
        p.write_text("".join(sorted(set("".join(old) + "".join(missing)))), encoding="utf-8")
    except OSError:
        pass                      # 只读目录之类就算了，不影响播放
