# -*- coding: utf-8 -*-
"""
config.py —— 全局配置
作用：集中所有可调参数，全部可用环境变量覆盖，Windows / 树莓派共用一套代码。
相关文件：被 audio.py / stt.py / llm.py / backend.py / tts.py / assistant.py 引用。
密钥单独放在 local_secrets.py（不进版本库），其余都有默认值。
"""
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent

try:                       # 密钥文件缺失时用环境变量，实在没有再报错
    from local_secrets import (CLOUD_ASR_KEY, CLOUD_ASR_SECRET, LLM_API_KEY,
                               MINIMAX_API_KEY, MINIMAX_VOICE_ID)
except ImportError:
    LLM_API_KEY = CLOUD_ASR_KEY = CLOUD_ASR_SECRET = ""
    MINIMAX_API_KEY = MINIMAX_VOICE_ID = ""


def _env(name, default):
    """读环境变量，空字符串按未设置处理（方便 set LLM_MODEL= 这种清空写法）。"""
    return os.getenv(name) or default


def _device(name):
    """音频设备：纯数字按索引传 int，否则按名字传 str，未设置传 None 走系统默认。"""
    v = (os.getenv(name) or "").strip()
    if not v:
        return None
    return int(v) if v.isdigit() else v


def _on(name, default="0"):
    return _env(name, default).lower() in ("1", "true", "on", "yes")


# ---------------- 音频 ----------------
SAMPLE_RATE = 16000                    # 16kHz：Vosk 小模型要求，也是 Pi 4 的舒适区
BLOCK = 1600                           # 每块 100ms
IN_DEVICE = _device("IN_DEVICE")       # 输入设备，None=系统默认；Pi 上常用 aplay -l 查
OUT_DEVICE = _device("OUT_DEVICE")     # 输出设备，和输入分开配，接 USB 麦+HDMI 音箱时必需

# 降噪开关：教室/宿舍这类有持续底噪的环境打开；安静环境建议关掉（降噪可能伤识别率）
DENOISE = _on("DENOISE")
DENOISE_STRENGTH = float(_env("DENOISE_STRENGTH", "1.0"))   # 1.0 保守，>1.5 会削出断续

# 断句与超时
VAD_THRESHOLD = float(_env("VAD_THRESHOLD", "0.02"))  # 音量超过它算"在说话"（0..1 归一化，0.02≈-34dBFS）
SILENCE_SEC = float(_env("SILENCE_SEC", "1.0"))  # 说完后静音多久算一句结束
MAX_SEC = float(_env("MAX_SEC", "12"))           # 单句最长录音
PREROLL_SEC = float(_env("PREROLL_SEC", "1.2"))  # 唤醒前预录，避免吃掉句首几个字
# 复核唤醒词时用的音频长度。"奶龙你好"能念到 1.7 秒，太短会截掉开头。
VERIFY_SEC = float(_env("VERIFY_SEC", "2.5"))
# 实时打印识别到的文字（听成什么样一眼就能看到）。嫌吵就设 STT_ECHO=0。
STT_ECHO = _on("STT_ECHO", "1")
# 唤醒后要不要应一声"我在"。
#   1（默认）：分开说最顺——「奶龙你好」→ 听到"我在" → 再说请求
#   0        ：不说应答音，适合"奶龙你好，我还有哪些作业没交"一口气说完，
#              因为播放应答的那 0.4 秒是不录音的，会把中间的半句吃掉
ACK = _on("ACK", "1")
DEBUG = _on("DEBUG")

# ---------------- 本地语音识别 (Vosk) ----------------
VOSK_MODEL = _env("VOSK_MODEL", str(ROOT / "models" / "vosk-model-small-cn-0.22"))
# 唤醒词，可以有多个（逗号/空格/竖线分隔），命中任意一个就算唤醒。
# 默认这串是实测出来的，不是拍脑袋写的：说"奶龙你好"时 Vosk 小模型会听成"哪种你好"
# （"奶龙"压根不在它词表里，所以永远拼不出这两个字），同音的几种都列上才稳。
# 想改就改这一行，别去动代码。
WAKE_WORDS = [w for w in re.split(
    r"[,，|、\s]+",
    _env("WAKE_WORD", "奶龙你好,哪种你好,那楼你好,奶农你好,奶牛你好,内容你好,你好小助手"),
) if w]

# 同音字族：语音识别对"奶龙"每次听成不同的字（哪笼/奶农/那句/奈龙…），
# 穷举别名追不上，所以再放一条正则，命中任意两个同音字也算唤醒。
# 设 WAKE_REGEX=0 关掉。
#   第一字：奶哪那乃奈   第二字：龙笼隆荣农牛
# 故意不含"内/容"——"内容"是常用词，含进去会误唤醒。
_wake_re = _env("WAKE_REGEX", "[奶哪那乃奈][龙笼隆荣农牛]")
WAKE_REGEX = "" if _wake_re.lower() in ("0", "off", "none", "") else _wake_re
# 拼音相似度阈值（0~1）。这是最管用的一层：
# "奶龙"不在模型词表里（"奶""龙"单字都在，但语言模型不认识这个词），
# 所以 Vosk 永远输出别的词，按字匹配只能一个个猜，按读音比才对。
# 实测：该唤醒的说法 0.67~0.92，正常说话 0.20~0.42，0.55 是两边中点。
# 老被误唤醒就调高，老是漏就调低。
WAKE_SIMILARITY = float(_env("WAKE_SIMILARITY", "0.55"))
# 唤醒词限定词表：能显著降 CPU，但只能用模型词表里已有的词。
# 例：WAKE_GRAMMAR="你好 小助手"（"你好""小助手"在词表内，实测可用）
# 留空 = 不限词表（最稳，但 CPU 高、误唤醒多）
WAKE_GRAMMAR = _env("WAKE_GRAMMAR", "")

# 指令识别引擎（唤醒始终用 Vosk，不受这个影响）：
#   auto    依次尝试：云（配了就用）→ whisper（装了就用）→ Vosk
#   vosk    只用 Vosk —— 最轻，树莓派上建议这个
#   whisper 强制 whisper —— 纯离线，安静环境比 Vosk 准
#   cloud   强制云识别，失败也会退回 whisper/vosk，不会罢工
# 实测错字率（12 句本领域话术）：云 2.1% / whisper-base 7.1% / vosk 9.2%
ASR_ENGINE = _env("ASR_ENGINE", "auto").lower()
WHISPER_SIZE = _env("WHISPER_SIZE", "base")   # base 是实测的甜点：7.1% 错字 / 0.8 秒

# ---------------- 音色复刻（MiniMax，只在 gen_voice.py 里用） ----------------
# 复刻是"离线一次性"的：跑一次把 217 条素材换成奶龙音色，运行时不再联网。
MINIMAX_API_KEY = _env("MINIMAX_API_KEY", MINIMAX_API_KEY)
MINIMAX_VOICE_ID = _env("MINIMAX_VOICE_ID", MINIMAX_VOICE_ID or "nailongDemo01")
MINIMAX_MODEL = _env("MINIMAX_MODEL", "speech-2.8-hd")
MINIMAX_HOST = _env("MINIMAX_HOST", "https://api.minimax.cn")

# ---------------- 联网语音识别（ASR_ENGINE=cloud 时才用） ----------------
# baidu  ：纯 HTTP，免费 5 万次/天，国内直连；KEY 是 API Key，SECRET 是 Secret Key
# openai ：任何 OpenAI 兼容的 /audio/transcriptions（SiliconFlow / Groq / 自建 faster-whisper）
CLOUD_ASR_PROVIDER = _env("CLOUD_ASR_PROVIDER", "baidu").lower()
CLOUD_ASR_KEY = _env("CLOUD_ASR_KEY", CLOUD_ASR_KEY)
CLOUD_ASR_SECRET = _env("CLOUD_ASR_SECRET", CLOUD_ASR_SECRET)   # 只有 baidu 要
CLOUD_ASR_BASE = _env("CLOUD_ASR_BASE", "https://api.siliconflow.cn/v1")
CLOUD_ASR_MODEL = _env("CLOUD_ASR_MODEL", "FunAudioLLM/SenseVoiceSmall")
# 百度认哪个模型决定走哪个端点，混用会报 3302：
#   1537 = 普通话增强版（标准版）→ vop.baidu.com/server_api
#   80001 = 语音识别极速版       → vop.baidu.com/pro_api
CLOUD_ASR_DEV_PID = _env("CLOUD_ASR_DEV_PID", "1537")

# ---------------- LLM（OpenAI 兼容接口） ----------------
LLM_BASE_URL = _env("LLM_BASE_URL", "https://models.sjtu.edu.cn/api/v1").rstrip("/")
LLM_API_KEY = _env("LLM_API_KEY", LLM_API_KEY)
LLM_MODEL = _env("LLM_MODEL", "deepseek-chat")   # 用 chat 不用 reasoner：要的是快、准、便宜
LLM_TIMEOUT = float(_env("LLM_TIMEOUT", "20"))

# ---------------- 电脑端服务（队友的 aiwozhu-app） ----------------
# 奶龙只是终端，数据在电脑上：电脑跑 app/server.py，终端连过去。
# 树莓派要连 PC 时把 BACKEND_URL 改成电脑的 IP（同网段或 ZeroTier 都行）。
BACKEND_URL = _env("BACKEND_URL", "http://127.0.0.1:5173")
# 电脑端从别的机器访问 /api/* 要口令（头名 X-Access-Key），
# 值对应电脑端的 NAIWOZHU_ACCESS_KEY。本机访问不用填。
BACKEND_TOKEN = _env("BACKEND_TOKEN", "")
BACKEND_TIMEOUT = float(_env("BACKEND_TIMEOUT", "10"))
# 交我办快照超过这么多小时没更新，播报日程时补一句"数据可能旧了"。
# 交我办的会话在电脑浏览器里，终端只能拿到快照，这一句是必要的诚实。
STALE_HOURS = float(_env("STALE_HOURS", "24"))

# ---------------- 语音拼接 ----------------
VOICE_DIR = Path(_env("VOICE_DIR", str(ROOT / "resources" / "voice")))
GAP_SEC = float(_env("GAP_SEC", "0.05"))   # 片段间留白，太密会连成一坨

# 临时兜底：奶龙素材还没生成时，用系统自带 TTS 念出来，先把对话链路验通
SYSTEM_TTS = _on("SYSTEM_TTS")
