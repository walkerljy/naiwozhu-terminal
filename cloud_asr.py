# -*- coding: utf-8 -*-
"""
cloud_asr.py —— 联网语音识别（可选，第三级引擎）

本地模型再优化也有上限，接云端 ASR 通常是准确率提升最大的一步。
这里只做一层薄适配，**换供应商只改配置不改代码**。

支持的供应商（`CLOUD_ASR_PROVIDER`）：

  baidu   百度短语音识别 —— 纯 HTTP，两跳：换 token → POST base64 音频。
          免费额度 5 万次/天，国内直连，最容易拿到 key。
  openai  OpenAI 兼容的 /audio/transcriptions —— 覆盖 OpenAI、SiliconFlow、
          Groq、以及自己搭的 faster-whisper 服务，换个 BASE_URL 就能用。

配置（都在 config.py 里，有环境变量可覆盖）：
    CLOUD_ASR_PROVIDER   baidu / openai
    CLOUD_ASR_KEY        百度是 API Key；openai 兼容的是 token
    CLOUD_ASR_SECRET     百度要 Secret Key；openai 兼容的留空
    CLOUD_ASR_BASE       openai 兼容的地址，默认 https://api.siliconflow.cn/v1
    CLOUD_ASR_MODEL      openai 兼容的模型名，默认 FunAudioLLM/SenseVoiceSmall

单独验证 key 是否可用（不用起整个助手）：
    python cloud_asr.py 某段音频.wav

失败一律返回 None，由调用方退回本地引擎 —— 演示现场断网不该直接罢工。
"""
import base64
import io
import json
import time
import urllib.error
import urllib.request
import wave

import config

_token = {"value": "", "expire_at": 0.0}
_BOUNDARY = "----naiwozhuAsrBoundary"

# 百度的错误码不说人话，翻译一下 —— 这几个是接入时一定会撞上的
_BAIDU_ERRORS = {
    3300: "请求参数不对（缺 speech/len 之类）。原样跑百度示例能复现：没有音频时它也报 3300",
    3301: "音频质量太差或格式不支持",
    3302: "这个应用没有该模型的权限。检查端点与 dev_pid 是否配对："
          "标准版 1537 → server_api；极速版 80001 → pro_api",
    3303: "百度服务端问题，重试即可",
    3304: "QPS 超限（同一秒请求太多），等一秒重试",
    3305: "当天请求量已超限 —— 去控制台看「语音识别」的免费额度有没有领取/用完，"
          "以及账号是否已实名认证。这个错误跟音频内容无关",
    3307: "服务端问题，重试",
    3308: "音频过长（超过 60 秒）",
    3309: "音频过大",
    3310: "音频过短",
    3311: "采样率不对（要 16k）",
    3312: "音频格式不对",
}


# ---------------------------------------------------------------- 工具
def wav_bytes(pcm):
    """int16 单声道 16k 采样 → 完整 wav 字节。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(config.SAMPLE_RATE)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def _post(url, data, headers, timeout=15):
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def configured():
    """这个引擎有没有配齐。"""
    if config.CLOUD_ASR_PROVIDER == "baidu":
        return bool(config.CLOUD_ASR_KEY and config.CLOUD_ASR_SECRET)
    return bool(config.CLOUD_ASR_KEY)


# ---------------------------------------------------------------- 供应商
def _baidu(pcm):
    """百度短语音识别：先换 access_token（有效期 30 天），再 POST 音频。

    两个端点别混用，混用会返回 3302「No permission to access data」：
        标准版  dev_pid=1537（1536 也行）→ vop.baidu.com/server_api
        极速版  dev_pid=80001            → vop.baidu.com/pro_api
    """
    now = time.time()
    if not _token["value"] or now >= _token["expire_at"]:
        url = ("https://aip.baidubce.com/oauth/2.0/token?grant_type=client_credentials"
               f"&client_id={config.CLOUD_ASR_KEY}&client_secret={config.CLOUD_ASR_SECRET}")
        data = _post(url, b"", {"Content-Type": "application/json"})
        if "access_token" not in data:
            raise RuntimeError(f"百度换 token 失败：{data.get('error_description') or data}")
        _token["value"] = data["access_token"]
        _token["expire_at"] = now + int(data.get("expires_in", 2592000)) - 600

    pid = int(config.CLOUD_ASR_DEV_PID)
    host = "vop.baidu.com/pro_api" if pid == 80001 else "vop.baidu.com/server_api"
    # 极速版只收 pcm/wav，这里发裸 PCM（不带 wav 头），和百度示例一致
    raw = pcm.tobytes()
    body = json.dumps({
        "format": "pcm",
        "rate": config.SAMPLE_RATE,
        "channel": 1,
        "dev_pid": pid,
        "cuid": "naiwozhu",
        "token": _token["value"],
        "speech": base64.b64encode(raw).decode("ascii"),
        "len": len(raw),
    }).encode("utf-8")
    for attempt in (0, 1):                        # 3304 是 QPS 超限，等一下重试一次
        data = _post(f"https://{host}", body, {"Content-Type": "application/json"})
        if data.get("err_no") == 3304 and attempt == 0:
            time.sleep(1.2)
            continue
        break
    code = data.get("err_no")
    if code != 0:
        raise RuntimeError(f"百度 {code}：{_BAIDU_ERRORS.get(code) or data.get('err_msg')}")
    return "".join(data.get("result") or [])


def _openai(pcm):
    """OpenAI 兼容的 /audio/transcriptions（multipart）。"""
    raw = wav_bytes(pcm)
    parts = []
    for name, value in (("model", config.CLOUD_ASR_MODEL), ("language", "zh")):
        parts.append(f"--{_BOUNDARY}\r\nContent-Disposition: form-data; "
                     f'name="{name}"\r\n\r\n{value}\r\n'.encode("utf-8"))
    parts.append(f"--{_BOUNDARY}\r\nContent-Disposition: form-data; "
                 f'name="file"; filename="a.wav"\r\n'
                 f"Content-Type: audio/wav\r\n\r\n".encode("utf-8") + raw + b"\r\n")
    parts.append(f"--{_BOUNDARY}--\r\n".encode("utf-8"))
    data = _post(
        config.CLOUD_ASR_BASE.rstrip("/") + "/audio/transcriptions", b"".join(parts),
        {"Content-Type": f"multipart/form-data; boundary={_BOUNDARY}",
         "Authorization": "Bearer " + config.CLOUD_ASR_KEY})
    return (data.get("text") or "").strip()


_PROVIDERS = {"baidu": _baidu, "openai": _openai}


# ---------------------------------------------------------------- 对外
def transcribe(pcm):
    """识别一段 int16 单声道音频；失败返回 None（调用方退回本地引擎）。"""
    fn = _PROVIDERS.get(config.CLOUD_ASR_PROVIDER)
    if fn is None:
        print(f"[asr] 不认识的云供应商：{config.CLOUD_ASR_PROVIDER}")
        return None
    try:
        return (fn(pcm) or "").strip() or None
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")[:120]
        print(f"[asr] 云识别 HTTP {e.code}：{detail}")
    except Exception as e:
        print(f"[asr] 云识别失败：{type(e).__name__}: {e}")
    return None


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        raise SystemExit("用法：python cloud_asr.py 音频.wav")
    with wave.open(sys.argv[1], "rb") as w:
        pcm = __import__("numpy").frombuffer(w.readframes(w.getnframes()), dtype="int16")
    print("供应商:", config.CLOUD_ASR_PROVIDER, "| 配齐:", configured())
    print("识别结果:", transcribe(pcm))
