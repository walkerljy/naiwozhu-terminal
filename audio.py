# -*- coding: utf-8 -*-
"""
audio.py —— 音频底层
作用：麦克风录音流（可选降噪）、扬声器播放、提示音。
主要模块：record_stream()（逐块产出「送识别的音频块 + 原始音量」）、play()、beep()、Denoiser。
相关文件：参数来自 config.py；被 stt.py / tts.py / assistant.py 调用。
"""
import numpy as np
import sounddevice as sd

import config


class Denoiser:
    """简化版谱减降噪。

    思路：用指数平滑估计每个频点的长期幅度（≈ 噪声底），再从当前块里减掉。
    对空调、风扇、人群持续嗡嗡声有效；对忽大忽小的人声干扰帮助有限——
    那种情况真正管用的是麦克风靠近嘴、或用指向麦。
    减完保留 10% 残余谱，避免把语音削成断续反而害了 Vosk 识别。
    """

    def __init__(self, n, strength=1.0, alpha=0.02):
        self.n = n
        self.nf = None                       # 噪声幅度谱
        self.strength = strength
        self.alpha = alpha                   # 越大跟踪越快、但越容易被语音带跑
        r = max(1, int(0.01 * config.SAMPLE_RATE))
        self.fade = np.ones(n, np.float32)   # 块首尾淡入淡出，避免拼接处"咔"声
        self.fade[:r] = np.linspace(0, 1, r)
        self.fade[-r:] = np.linspace(1, 0, r)

    def __call__(self, x):
        spec = np.fft.rfft(x)
        mag = np.abs(spec)
        if self.nf is None:
            self.nf = mag
            return x * self.fade
        self.nf = (1 - self.alpha) * self.nf + self.alpha * mag
        keep = np.maximum(mag - self.strength * self.nf, 0.1 * mag)
        out = np.fft.irfft(spec * (keep / np.maximum(mag, 1e-9)), self.n)
        return out.astype(np.float32) * self.fade


def _resample(pcm, in_rate, out_rate):
    """把 in_rate 的 int16 降/升采样到 out_rate。

    整数倍直接按组取平均（48k→16k 就是每 3 个取均值，顺带做了抗混叠）；
    非整数倍走线性插值。语音场景够用，不引入 scipy 依赖。
    """
    if in_rate == out_rate or len(pcm) == 0:
        return pcm
    if in_rate % out_rate == 0:
        k = in_rate // out_rate
        usable = len(pcm) // k * k
        return pcm[:usable].reshape(-1, k).mean(axis=1).astype(np.int16)
    idx = np.arange(0, len(pcm), in_rate / out_rate)
    return np.interp(idx, np.arange(len(pcm)), pcm).astype(np.int16)


_RATE = {"in": None}          # 这次开成功过的采样率，下次直接用它，不再每回重试 16k


def _open_input():
    """打开输入流；设备不支持 16k 就退到它支持的采样率（软件重采样回 16k）。

    为什么要这么兜：不少 USB 声卡（如 C-Media/PCM2902 那类）只支持
    44.1k/48k，PortAudio 直接开 16k 会报 Invalid sample rate（-9997），
    程序连麦克风都打不开——而 Vosk 只吃 16k。
    """
    candidates = [config.SAMPLE_RATE, 48000, 44100]
    if _RATE["in"] in candidates:                 # 上次成功的优先
        candidates.remove(_RATE["in"])
        candidates.insert(0, _RATE["in"])
    last = None
    for rate in candidates:
        block = int(config.BLOCK * rate / config.SAMPLE_RATE)
        try:
            s = sd.InputStream(samplerate=rate, blocksize=block, channels=1,
                               dtype="int16", device=config.IN_DEVICE)
            s.start()
            if rate != config.SAMPLE_RATE:
                print(f"[audio] 麦克风不支持 {config.SAMPLE_RATE}Hz，改用 {rate}Hz 采集"
                      f"（软件重采样到 {config.SAMPLE_RATE}Hz）", flush=True)
            _RATE["in"] = rate
            return s, rate, block
        except Exception as e:                    # 换下一个候选采样率
            last = e
    raise last


def record_stream(dur=None):
    """逐块录音，产出 (int16 音频块, 音量 rms)。

    第一项必须是 **int16 的 PCM**：Vosk 的 AcceptWaveform 只认 16bit 整数样本，
    喂 float32 进去它会当成整数解读，结果是听不清人话（这里踩过坑）。
    第二项是归一到 0..1 的音量，用来和 VAD_THRESHOLD 比。降噪只作用在送识别的那路，
    音量始终按原始信号算，所以开关降噪不会改变断句行为。

    采样率：对外永远是 config.SAMPLE_RATE（Vosk 要 16k），设备支持不了就在内部
    按它支持的率采、再重采样回 16k。
    """
    total = int(config.SAMPLE_RATE * dur) if dur else None
    dnr = Denoiser(config.BLOCK, config.DENOISE_STRENGTH) if config.DENOISE else None
    n = 0
    s, in_rate, block = _open_input()
    try:
        while total is None or n < total:
            data, _ = s.read(block)
            pcm = _resample(data.reshape(-1), in_rate, config.SAMPLE_RATE)
            n += len(pcm)
            raw = pcm.astype(np.float32) / 32768.0
            rms = float(np.sqrt(np.mean(raw * raw)))
            if dnr is None:
                yield pcm, rms                      # 不降噪就直接把原始 int16 送出去，不做无谓转换
            else:
                yield np.clip(dnr(raw) * 32767, -32768, 32767).astype(np.int16), rms
    finally:
        s.stop()
        s.close()


def play(audio):
    """播放 float32(-1..1) 或 int16 数组。"""
    if audio is None or len(audio) == 0:
        return
    sd.play(np.asarray(audio), config.SAMPLE_RATE, device=config.OUT_DEVICE)
    sd.wait()


def beep(freq=880, ms=120):
    """短提示音：告诉用户"我听到了，你说吧"。"""
    t = np.arange(int(config.SAMPLE_RATE * ms / 1000)) / config.SAMPLE_RATE
    return (0.25 * np.sin(2 * np.pi * freq * t)).astype(np.float32)
