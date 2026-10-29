# -*- coding: utf-8 -*-
"""
asr_bench.py —— 语音识别引擎对比测评（开发用，不参与运行时）

为什么要它：换模型/换参数之前得先知道现在错多少。凭感觉调是白费功夫。

做法：把一批本领域会说的句子用系统语音合成成 16k wav，再叠加不同程度的噪声
（模拟麦克风离嘴远近、房间反射），然后用各个引擎识别，算**字符错误率 CER**：

    CER = 编辑距离(识别结果, 原句) / 原句字数

用法：
    python asr_bench.py                     # 跑默认的引擎组合
    python asr_bench.py --wavs-only         # 只生成测试音频，方便人工听
    python asr_bench.py --whisper small     # 只测 whisper small

注意：合成语音是"太干净"的，绝对 CER 会比真人对着麦克风说话低。
     它只适合**相对比较**（A 引擎 vs B 引擎、有噪 vs 无噪），别拿绝对值对外说。
"""
import json
import subprocess
import sys
import time
import wave
from pathlib import Path

import numpy as np

import config

HERE = Path(__file__).resolve().parent
TMP = HERE / "_asr"
SR = 16000

# 演示/使用中真的会说的句子（覆盖问法里的各种句式）
SENTENCES = [
    "奶龙你好",
    "我还有哪些作业没交",
    "我今天有什么课",
    "明天有什么要交的",
    "这周有什么安排",
    "高等数学有什么作业",
    "我明天有哪些日程",
    "把明天的课改到下午",
    "下一个作业什么时候交",
    "今天有几节课",
    "帮我看看程序设计实验报告什么时候截止",
    "学生会例会在哪个教室",
]

# 模拟条件：干净 / 有噪 / 增益偏小（麦克风太远、系统增益低是常见的掉准确率原因）
CONDITIONS = [
    ("干净", None, 1.0),
    ("信噪15dB", 15.0, 1.0),
    ("信噪8dB", 8.0, 1.0),
    ("干净但音量小", None, 0.15),
]


def synth(text, path):
    """系统语音合成 16k/16bit/单声道 wav。"""
    ps = ("Add-Type -AssemblyName System.Speech; "
          "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
          "$f = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, "
          "[System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, "
          "[System.Speech.AudioFormat.AudioChannel]::Mono); "
          f"$s.SetOutputToWaveFile('{path}', $f); "
          f"$s.Speak('{text}'); $s.Dispose()")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True)


def load(path):
    with wave.open(str(path), "rb") as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)


def degrade(pcm, snr_db, gain):
    """调增益 + 叠加高斯白噪声，用来模拟不同录音条件。"""
    x = (pcm.astype(np.float32) / 32768.0) * gain
    if snr_db is not None:
        p_sig = float(np.mean(x ** 2))
        if p_sig > 0:
            p_noise = p_sig / (10 ** (snr_db / 10))
            x = x + np.random.normal(0, np.sqrt(p_noise), len(x)).astype(np.float32)
    return np.clip(x * 32768.0, -32768, 32767).astype(np.int16)


def cer(ref, hyp):
    """字符错误率：去掉标点空白后算编辑距离。"""
    clean = lambda s: "".join(c for c in s if c not in "，。！？；：、,.!?;:… ")
    r, h = clean(ref), clean(hyp)
    if not r:
        return 0.0 if not h else 1.0
    prev = list(range(len(h) + 1))
    for i, rc in enumerate(r, 1):
        cur = [i]
        for j, hc in enumerate(h, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (rc != hc)))
        prev = cur
    return prev[-1] / len(r)


# ---------------- 引擎 ----------------

def make_vosk(model_dir):
    from vosk import KaldiRecognizer, Model, SetLogLevel

    SetLogLevel(-1)
    model = Model(str(model_dir))

    def run(pcm):
        rec = KaldiRecognizer(model, SR)
        parts = []
        for i in range(0, len(pcm), 3200):
            if rec.AcceptWaveform(pcm[i:i + 3200].tobytes()):
                parts.append(json.loads(rec.Result()).get("text", ""))
        parts.append(json.loads(rec.FinalResult()).get("text", ""))
        return "".join(parts).replace(" ", "")
    return run


def make_whisper(size):
    from faster_whisper import WhisperModel

    model = WhisperModel(size, device="cpu", compute_type="int8", num_workers=4)

    def run(pcm):
        audio = pcm.astype(np.float32) / 32768.0
        segs, _ = model.transcribe(
            audio, language="zh", beam_size=5, vad_filter=False,
            condition_on_previous_text=False,
            # 不写这句，whisper 中文经常输出繁体（學生會 vs 学生会），
            # 字符级测评会被判成全错，实际发音是对的
            initial_prompt="以下是普通话的句子，请用简体中文。")
        return "".join(s.text for s in segs).strip()
    return run


def make_cloud():
    """百度短语音（cloud_asr）。调用有 QPS 限制，每句之间要歇一下。"""
    import cloud_asr

    def run(pcm):
        out = cloud_asr.transcribe(pcm)
        time.sleep(0.7)                     # 避开 QPS 限流
        return out or ""
    return run


def main():
    args = sys.argv[1:]
    TMP.mkdir(exist_ok=True)
    np.random.seed(0)

    # 1) 生成并缓存测试音频
    wavs = []
    for i, text in enumerate(SENTENCES):
        p = TMP / f"s{i:02d}.wav"
        if not p.exists():
            synth(text, p)
        wavs.append((text, load(p)))
    print(f"测试音频就绪：{len(wavs)} 句\n")

    if "--wavs-only" in args:
        print("音频在", TMP)
        return

    # 2) 选引擎：vosk-small 始终在场（当基线），whisper 通过 --whisper 追加
    engines = []
    small = HERE / "models" / "vosk-model-small-cn-0.22"
    large = TMP / "vosk-model-cn-0.22"
    if small.exists():
        engines.append(("vosk-small", make_vosk(small)))
    if large.exists():
        engines.append(("vosk-large", make_vosk(large)))
    if "--whisper" in args:
        size = args[args.index("--whisper") + 1]
        engines.append((f"whisper-{size}", make_whisper(size)))
    if "--cloud" in args:
        import cloud_asr
        if cloud_asr.configured():
            engines.append((f"cloud-{config.CLOUD_ASR_PROVIDER}", make_cloud()))
        else:
            print("云识别没配齐，跳过")

    if not engines:
        print("没有可用的引擎")
        return

    # 3) 逐条件跑
    print(f"{'条件':<14}" + "".join(f"{name:>14}" for name, _ in engines))
    print("-" * (14 + 14 * len(engines)))
    details = {}
    for cname, snr, gain in CONDITIONS:
        row = f"{cname:<14}"
        for ename, run in engines:
            errs = []
            for text, pcm in wavs:
                hyp = run(degrade(pcm, snr, gain))
                errs.append(cer(text, hyp))
            avg = sum(errs) / len(errs)
            details[(cname, ename)] = (avg, errs)
            row += f"{avg * 100:>13.1f}%"
        print(row)

    # 4) 干净条件下逐句看错在哪
    print("\n=== 干净条件逐句（错误率最高的 4 句）===")
    for ename, _ in engines:
        avg, errs = details[("干净", ename)]
        worst = sorted(zip(errs, SENTENCES), reverse=True)[:4]
        print(f"\n[{ename}] 平均 CER {avg * 100:.1f}%")
        for e, s in worst:
            print(f"  {e * 100:5.1f}%  {s}")

    if "--show" in args:
        print("\n=== 干净条件逐句对照（原句 → 识别）===")
        for ename, run in engines:
            print(f"\n[{ename}]")
            for text, pcm in wavs:
                hyp = run(degrade(pcm, None, 1.0))
                print(f"  {text}\n    → {hyp}")


if __name__ == "__main__":
    main()
