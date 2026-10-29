# -*- coding: utf-8 -*-
"""
stt.py —— 本地离线语音识别（唤醒 + 识别一句话）

**两级解码**，因为两件事的要求正好相反：

  唤醒  要常驻、要流式、要便宜  →  Vosk 小模型（几十 MB，边听边出字）
  指令  只跑一次、要准          →  faster-whisper（更准，慢一点也值得）

实测（12 句本领域话术，干净音频，字符错误率）：

    vosk-small    9.2%  每段 2033ms
    whisper-base  7.1%  每段  828ms   ← 又准又快，所以默认用它
    whisper-small 6.2%  每段 2752ms   ← 更准但太慢

噪声环境下 vosk 反而更稳（8dB 信噪比：20% vs 26%），所以引擎可配：
`ASR_ENGINE=auto|vosk|whisper`。

主要模块：wait_for_wake()、capture()。
相关文件：录音来自 audio.py；参数来自 config.py；被 assistant.py 调用。
"""
import itertools
import json
import re
import time
from collections import deque
from pathlib import Path

import numpy as np
from vosk import KaldiRecognizer, Model, SetLogLevel

import config
from audio import record_stream

SetLogLevel(-1)          # 关掉 Vosk 自己的日志，免得刷屏

_model = None            # 懒加载：只有真的要听声音时才吃这 40MB 内存和几秒启动时间
_whisper = None
_WAKE = sorted((w.replace("您", "你") for w in config.WAKE_WORDS),
               key=len, reverse=True)   # 长的优先，剥离时不会只剥一半
_WAKE_RE = re.compile(config.WAKE_REGEX) if config.WAKE_REGEX else None

try:
    from pypinyin import lazy_pinyin as _lazy_pinyin
except ImportError:      # 没装就只走别名和正则（后果是唤醒更容易漏）
    _lazy_pinyin = None


def _pinyin(text):
    if _lazy_pinyin is None:
        return ""
    return "".join(_lazy_pinyin(text))


def _lev(a, b):
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _similar(a, b):
    """两串拼音的相似度（1 - 编辑距离/较长长度），0~1。"""
    if not a or not b:
        return 0.0
    return 1 - _lev(a, b) / max(len(a), len(b))


_WAKE_PY = [(w, _pinyin(w)) for w in _WAKE] if _lazy_pinyin else []
_hit_cache = {"text": None, "hit": None}


def _norm(text):
    """匹配前统一一下：识别成"您"的当"你"看（"奶农您好"就是这么漏掉的）。"""
    return text.replace("您", "你")


def _wake_hit(text):
    """命中哪个唤醒词就返回哪个，没命中返回 None。

    三层，从严到松：
      1. 精确别名 —— 最快，零成本
      2. 同音字族正则 —— 抓住"哪笼"这类
      3. 拼音相似度 ≥ WAKE_SIMILARITY —— 兜住所有没想到的误听

    第三层是重点：`奶龙` 不在模型的词表里（"奶"和"龙"单字都在，但语言模型
    不认识这个词），所以 Vosk 永远输出别的词。按字匹配只能一个个猜，
    按读音匹配才是正解 —— 实测"奶农你好/哪种你好/那楼你好/买龙尼好/海龙你好"
    相似度都在 0.75~0.92，而正常说话都 ≤ 0.42，中间有 0.25 的鸿沟。
    """
    if text == _hit_cache["text"]:
        return _hit_cache["hit"]
    hit = _wake_hit_uncached(text)
    _hit_cache.update(text=text, hit=hit)
    return hit


def _wake_hit_uncached(text):
    t = _norm(text)
    for wake in _WAKE:
        if wake in t:
            return wake
    if _WAKE_RE:
        m = _WAKE_RE.search(t)
        if m:
            return m.group(0)
    if _WAKE_PY and len(t) <= 12:
        py = _pinyin(t)
        best, score = None, 0.0
        for word, word_py in _WAKE_PY:
            s = _similar(py, word_py)
            if s > score:
                best, score = word, s
        if score >= config.WAKE_SIMILARITY:
            return best
    return None


def _weak_candidate(text):
    """弱信号：只听见了"你好/您好"，前面的"奶龙"没听出来。

    Vosk 对"你好"这种常用词几乎不会错，对"奶龙"这种词表外的词几乎必错。
    所以"你好"出现时先当成候选，交给第二级引擎（云/whisper）复核确认，
    确认不了就继续听，不打扰用户。实测"那句你好"就是这么漏掉的。
    """
    t = _norm(text)
    head = t[:6]
    return ("你好" in head or "您好" in head) and len(t) <= 12


def _verify(pcm):
    """用第二级引擎复核一段音频，返回命中的唤醒词；没命中或引擎不可用返回 None。"""
    got = _refine(pcm)
    if not got:
        return None
    hit = _wake_hit(got)
    if hit:
        print(f"[stt] 复核通过：{got!r}", flush=True)
    else:
        print(f"[stt] 复核没通过（听到的是 {got!r}），继续听", flush=True)
    return hit


def check():
    """启动时校验模型可用，早报错好过唤醒时才发现。"""
    _get_model()


def _get_model():
    global _model
    if _model is None:
        if not Path(config.VOSK_MODEL).exists():
            raise SystemExit(f"[stt] 缺少识别模型：{config.VOSK_MODEL}\n"
                             f"先运行：python download_model.py")
        _model = Model(config.VOSK_MODEL)
    return _model


def _new_rec(grammar=""):
    if grammar:
        return KaldiRecognizer(_get_model(), config.SAMPLE_RATE, grammar)
    return KaldiRecognizer(_get_model(), config.SAMPLE_RATE)


def _result(j):
    return json.loads(j).get("text", "").replace(" ", "")


def _partial(j):
    return json.loads(j).get("partial", "").replace(" ", "")


_echoed = ""          # 上一次已经打出来的文字，用来去重


def level(sec=2.0):
    """测这段时间的音量，返回 (平均, 峰值)。

    判断"麦克风到底有没有收到声音"用这个：峰值为 0 说明根本没信号
    （设备选错、被静音、系统隐私设置拦了），跟识别准确率无关。
    """
    vals = [rms for _, rms in record_stream(dur=sec)]
    if not vals:
        return 0.0, 0.0
    return sum(vals) / len(vals), max(vals)


def _echo(text):
    """把当前识别到的文字实时打出来（默认开；STT_ECHO=0 可关）。

    只在这个词变了的时候打一行，所以是"每识别出一个词打一行"，
    而不是每 100ms 刷一次同样的内容。flush=True 保证管道/重定向时也能立刻看到。
    """
    global _echoed
    if not config.STT_ECHO or not text or text == _echoed:
        return
    _echoed = text
    print(f"[stt] 听到: {text}", flush=True)


def _reset_echo():
    global _echoed
    _echoed = ""


def wait_for_wake(sec=None):
    """阻塞直到听到唤醒词，返回唤醒前预录的音频块（喂给 capture 用）。

    sec 给定时最多等这么久，超时返回 None（自检用；正式运行不传，一直等）。

    用 PartialResult 做触发：等 AcceptWaveform 的端点检测出来要等到用户停顿，
    那会把"唤醒词+指令"一口气说完的场景整句话都耗掉。
    """
    rec = _new_rec(config.WAKE_GRAMMAR)
    ring = deque(maxlen=max(1, int(config.PREROLL_SEC * config.SAMPLE_RATE / config.BLOCK)))
    # 复核用的缓冲要比预录长：一条"奶龙你好"能念到 1.7 秒，
    # 用 1.2 秒的 ring 去复核会把开头截掉（实测会变成"海龙你好"）。
    vring = deque(maxlen=max(1, int(config.VERIFY_SEC * config.SAMPLE_RATE / config.BLOCK)))
    deadline = time.monotonic() + sec if sec else None
    cand_at = 0.0                              # 弱信号第一次出现的时间
    last_verify = 0.0
    for block, _ in record_stream():
        ring.append(block)                     # 滚动预录，句首几个字就靠它救回来
        vring.append(block)
        if rec.AcceptWaveform(block.tobytes()):
            text = _result(rec.Result())
        else:
            text = _partial(rec.PartialResult())
        _echo(text)

        hit = _wake_hit(text)                  # 强信号：别名 / 同音字族，直接认
        if hit:
            print(f"[stt] 唤醒：{hit}", flush=True)
            _reset_echo()
            return list(ring)

        # 弱信号：只听见"你好"。等短语念完（0.6 秒后）拿复核缓冲去确认，
        # 而且 1.5 秒内最多复核一次——否则每个音频块都会发一次云请求。
        if _weak_candidate(text):
            now = time.monotonic()
            if not cand_at:
                cand_at = now
            elif now - cand_at >= 0.6 and now - last_verify >= 1.5:
                last_verify = now
                if _verify(np.concatenate(vring)):
                    _reset_echo()
                    return list(ring)
        else:
            cand_at = 0.0

        if deadline and time.monotonic() > deadline:
            return None


def capture(ring=(), ack=None):
    """录一句话并返回文字。

    ring 里的预录会先灌进识别器，所以唤醒词本身也会被再识别一遍，
    返回前要去掉它（_strip_wake）。说完后静音 SILENCE_SEC 秒就结束；
    一直不说话就一直录到 MAX_SEC，不会提前放弃——
    提前放弃看着省时间，实际会把没踩到阈值的那句话整句吃掉。

    ack 是一个"应一声"的回调（播"我在"）。为什么放在这里判断：
    用户有两种说法——
      分开说：「奶龙你好」…停顿…「我还有哪些作业没交」→ 该应一声
      连着说：「奶龙你好，我还有哪些作业没交」→ 正说着呢，应一声会盖住他半句话
    所以先偷看唤醒后 0.3 秒：有声音就别应，直接录；安静才应。

    引擎是两级里的第二级：Vosk 一边录一边出字（给用户实时反馈），
    录完再用第二级引擎重识一遍整段音频（更准）。
    """
    rec = _new_rec()
    blocks = list(ring)                            # 整段音频留一份，给第二级用
    parts = []
    for block in ring:
        rec.AcceptWaveform(block.tobytes())

    it = iter(record_stream(dur=config.MAX_SEC))
    head = []
    if ack is not None:
        for _ in range(3):                         # 3 × 100ms
            try:
                head.append(next(it))
            except StopIteration:
                break
        if not head or max(rms for _, rms in head) < config.VAD_THRESHOLD:
            ack()                                  # 他停了，正常应一声

    heard, quiet = False, 0.0
    for block, rms in itertools.chain(head, it):
        blocks.append(block)
        # 端点处的文字要先收下来，否则长句子只剩最后一段
        if rec.AcceptWaveform(block.tobytes()):
            seg = _result(rec.Result())
            parts.append(seg)
            _echo(seg)                             # 边说边显示
        else:
            _echo(_partial(rec.PartialResult()))
        if rms >= config.VAD_THRESHOLD:
            heard, quiet = True, 0.0
        elif heard:
            quiet += config.BLOCK / config.SAMPLE_RATE
            if quiet >= config.SILENCE_SEC:
                break
    tail = _result(rec.FinalResult())
    parts.append(tail)
    _echo(tail)

    text = "".join(parts)
    if heard:
        # heard 为假时（压根没人说话）不送第二级：
        # whisper 在纯静音上会凭空编一句，云接口也会浪费一次额度。
        text = _refine(np.concatenate(blocks)) or text
    return _strip_wake(text)


# ---------------------------------------------------------------- 第二级识别
def _want_cloud():
    if config.ASR_ENGINE not in ("cloud", "auto"):
        return False
    import cloud_asr
    return cloud_asr.configured()


def _want_whisper():
    if config.ASR_ENGINE == "vosk":
        return False
    try:
        import faster_whisper  # noqa: F401
        return True
    except ImportError:
        return False


def engine_label():
    """给启动横幅和自检用的一句话。"""
    if _want_cloud():
        return f"cloud({config.CLOUD_ASR_PROVIDER})"
    if _want_whisper():
        return f"whisper-{config.WHISPER_SIZE}"
    return "vosk"


def _refine(pcm):
    """第二级识别：云优先，失败退回 whisper；都没有就返回 None（用 Vosk 的结果）。"""
    if _want_cloud():
        import cloud_asr
        got = cloud_asr.transcribe(pcm)
        if got:
            return got
        print("[stt] 云识别没成功，退回本地引擎", flush=True)
    if _want_whisper():
        return _transcribe(pcm)
    return None


def _transcribe(pcm):
    """用 faster-whisper 重识整段指令音频。"""
    global _whisper
    if _whisper is None:
        from faster_whisper import WhisperModel
        print(f"[stt] 加载 whisper-{config.WHISPER_SIZE}（只用一次，之后常驻）", flush=True)
        _whisper = WhisperModel(config.WHISPER_SIZE, device="cpu",
                                compute_type="int8", num_workers=4)
    segs, _ = _whisper.transcribe(
        pcm.astype(np.float32) / 32768.0, language="zh", beam_size=5,
        condition_on_previous_text=False, vad_filter=False,
        # 不加这句，whisper 中文经常输出繁体（"學生會" vs "学生会"）
        initial_prompt="以下是普通话的句子，请用简体中文。")
    return "".join(s.text for s in segs).strip()


_EDGE_PUNCT = "，,。.？?!！：:；;、…~ \t\n"


def _tidy(text):
    """去掉首尾标点和空白。whisper 会自带标点（"我还有哪些作业没交?"）。"""
    return text.strip(_EDGE_PUNCT)


def _strip_wake(text):
    """预录音频里带着唤醒词（如"奶农您好我还有…"），把它去掉。

    四种情况都得接住，因为同一段音频在不同引擎下听出来的东西差得很远：

      Vosk       "哪种你好我今天…"      → 别名/字族正则
      Vosk       "奶农您好我还有…"      → 把"您"当"你"
      Whisper    "买龙尼好,我明天…"      → 前两个字的声母韵母都对不上，只能靠"以你好结尾"
      Whisper    ",我今天有计节课。"     → 唤醒词整个消失，只剩一个逗号
    """
    t = _norm(text)

    # 1) 精确别名
    for wake in _WAKE:
        for n in range(len(wake), 1, -1):
            if t.startswith(wake[-n:]):
                return _tidy(t[n:])

    # 2) 同音字族正则
    if _WAKE_RE:
        m = _WAKE_RE.search(t[:8])
        if m:
            return _tidy(re.sub(r"^(?:你|您)[好号]", "", t[m.end():]))

    # 3) 兜底：唤醒词一定以"你好/您好"收尾，开头几个字里出现"好"就截到它为止
    for ch in "好号":
        pos = t[:6].find(ch)
        if pos >= 0:
            return _tidy(t[pos + 1:])

    # 4) 唤醒词整个没识别出来，只剩标点
    return _tidy(t)
