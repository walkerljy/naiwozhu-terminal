# -*- coding: utf-8 -*-
"""
gen_voice.py —— 离线一次性生成语音素材（运行时不需要它）

文件名就是它读的文字，tts.py 运行时按「最长匹配」取用：
    resources/voice/第一节是.wav          ← 模板固定片段
    resources/voice/高等数学.wav          ← 从接口取到的课名
    resources/voice/东上院四一五.wav      ← 从接口取到的教室
    resources/voice/你.wav                ← 单字兜底

用法：
    python gen_voice.py --list                            只列出要生成哪些片段，不联网
    python gen_voice.py --from-api --list                 连交我办，把真实课名/教室也算进去
    python gen_voice.py --from-api --provider cmd         正式素材走这条
    python gen_voice.py --provider edge                   用 edge-tts 占位音色，先把链路跑通

接自己的奶龙引擎：
    TTS_CMD="python nailong.py --text {text} --out {out}" python gen_voice.py --provider cmd
    约定命令把结果写成 16bit / 16kHz / 单声道 wav（引擎输出别的格式就在命令里顺手转掉）。

加料（待办/日程标题里可能出现的词）：
    python gen_voice.py --extra 常用词.txt --provider cmd
"""
import asyncio
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import config
from tts import PUNCT, TEMPLATES, speech_text

# 中文里没有"奶龙"这个音色。cartoon 标签里最接近的是 Xiaoyi（卡通女声）；
# 抬一点音高会更稚气。挑好之后把这三个值固定下来即可。
EDGE_VOICE = os.getenv("EDGE_VOICE", "zh-CN-XiaoyiNeural")
EDGE_PITCH = os.getenv("EDGE_PITCH", "+0Hz")
EDGE_RATE = os.getenv("EDGE_RATE", "+0%")
# MiniMax 的 RPM 限制很紧（实测 0.3 秒一条会大面积 1002 限流），这里放慢到约 20 次/分钟
MINIMAX_DELAY = float(os.getenv("MINIMAX_DELAY", "2.6"))
# 逐字兜底的字表：
#   数字与单位               零一二三四五六七八九十百千万两号点分秒年月日个第周节
#   时间说法（human_time）    今天明后上午下中晚凌晨
#   "还剩多久"（电脑端返回）  还有已过期小时
CORE = ("零一二三四五六七八九十百千万两号点分秒年月日个第周节"
        "今天明后上午下中晚凌晨有没有已过期小时")


def segments():
    """模板里的固定句式片段。

    无槽位的模板整句生成（语气最自然）；带槽位的按 {槽位} 切开，再按标点断成小片段。
    """
    out = set()
    for tpl in TEMPLATES.values():
        if "{" not in tpl:
            out.add(tpl)
            continue
        for chunk in tpl.replace("{", "\x00{").replace("}", "}\x00").split("\x00"):
            if not chunk or "{" in chunk:
                continue
            for frag in re.split(f"[{PUNCT}]", chunk):
                if frag.strip():
                    out.add(frag.strip())
    return sorted(out)


def items(extra=()):
    """要生成的片段清单。

    每条都过一遍 speech_text()，保证和 tts.py 运行时算出来的匹配键完全一致
    （"东中院1-106" 会变成 "东中院一一零六"，两边必须同一个函数）。
    """
    out, seen = [], set()
    for raw in list(segments()) + list(extra):
        key = speech_text(raw)
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def chars(extra=()):
    """逐字兜底要生成哪些字：片段用到的字 + 固定字表 + 上次播放时缺的字。"""
    pool = "".join(items(extra)) + CORE + _missing_from_last_run()
    return sorted({c for c in pool if "\u4e00" <= c <= "\u9fff"})


def _missing_from_last_run():
    """tts.speak() 播放时缺的字会记在 _missing.txt，下次生成自动带上。

    这样漏字能自己愈合：先跑一遍听/看哪里缺，再跑一次 gen_voice 就补上了。
    """
    p = config.VOICE_DIR / "_missing.txt"
    if not p.exists():
        return ""
    return "".join(p.read_text("utf-8").split())


def _minimax(text, out):
    """用复刻好的音色合成一条（MiniMax /v1/t2a_v2，非流式）。

    为什么是"离线一次性"：复刻和合成都按次计费、还依赖网络，
    而素材是固定的 217 条。生成一次存成 wav，运行时零延迟零依赖。
    """
    import json
    import urllib.error
    import urllib.request

    body = {
        "model": config.MINIMAX_MODEL,
        "text": text,
        "stream": False,
        "voice_setting": {"voice_id": config.MINIMAX_VOICE_ID,
                          "speed": 1.0, "vol": 1.0, "pitch": 0},
        # 直接要 16k 单声道 wav，正好是素材格式，省一层转码
        "audio_setting": {"sample_rate": config.SAMPLE_RATE,
                          "format": "wav", "channel": 1},
        "output_format": "hex",
    }
    req = urllib.request.Request(
        config.MINIMAX_HOST + "/v1/t2a_v2",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + config.MINIMAX_API_KEY})
    # 1002 = 触发 RPM 限流。素材是批量生成的，慢慢来就行，别把额度打爆。
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"MiniMax HTTP {e.code}: "
                               f"{e.read().decode('utf-8', 'ignore')[:120]}") from None
        code = (data.get("base_resp") or {}).get("status_code")
        if code == 0:
            break
        if code == 1002 and attempt < 4:
            wait = 5 * (attempt + 1)
            print(f"    限流，等 {wait}s 重试", flush=True)
            time.sleep(wait)
            continue
        raise RuntimeError(f"MiniMax {code}: {(data.get('base_resp') or {}).get('status_msg')}")
    Path(out).write_bytes(bytes.fromhex(data["data"]["audio"]))


def _edge(text, out):
    """edge-tts 出 mp3，再用 ffmpeg 转成 16k 单声道 wav。

    中文没有"奶龙"这个音色，cartoon 标签里最接近的是 Xiaoyi（卡通女声）。
    音高/语速可以用 EDGE_PITCH / EDGE_RATE 调（如 +40Hz、+10%），
    抬高一点会更稚气，接近动画角色的效果。
    """
    import edge_tts

    with tempfile.TemporaryDirectory() as td:
        mp3 = Path(td) / "a.mp3"

        async def run():
            await edge_tts.Communicate(text, EDGE_VOICE,
                                       pitch=EDGE_PITCH, rate=EDGE_RATE).save(str(mp3))

        asyncio.run(run())
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(mp3),
                        "-ar", str(config.SAMPLE_RATE), "-ac", "1",
                        "-acodec", "pcm_s16le", str(out)], check=True)


def _cmd(text, out):
    """你自己的引擎：把 TTS_CMD 里的 {text} {out} 换掉直接执行。"""
    tpl = os.getenv("TTS_CMD", "")
    if not tpl:
        raise SystemExit("[gen_voice] 用 --provider cmd 需要先设 TTS_CMD，见本文件开头说明")
    subprocess.run(tpl.format(text=text, out=str(out)), shell=True, check=True)


def _sapi(texts, outdir):
    """Windows 自带语音合成，一次性批量写成 16k/16bit/单声道 wav。

    只为在没有奶龙引擎时先把整条链路跑出声。音色是系统里的（Huihui），不是奶龙；
    奶龙引擎就绪后换 --provider cmd 重跑一遍即可，素材格式完全一样。

    一次 PowerShell 进程处理一整批，比每条起一个进程快一个数量级。
    """
    if sys.platform != "win32":
        raise SystemExit("[gen_voice] sapi 只有 Windows 有；别的系统用 --provider cmd")
    listfile = Path(outdir) / "_voices.txt"
    listfile.write_text("\n".join(texts), encoding="utf-8")
    script = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "$f = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, "
        "[System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, "
        "[System.Speech.AudioFormat.AudioChannel]::Mono); "
        "$n = 0; "
        f"Get-Content -LiteralPath '{listfile}' -Encoding UTF8 | ForEach-Object {{ "
        "  if ($_.Trim() -ne '') { "
        f"    $s.SetOutputToWaveFile((Join-Path '{outdir}' ($_ + '.wav')), $f); "
        "    $s.Speak($_.Trim()); $n++ "
        "  } }; "
        "$s.Dispose(); Write-Host \"sapi 生成 $n 条\"")
    subprocess.run(["powershell", "-NoProfile", "-Command", script], check=True)
    listfile.unlink(missing_ok=True)


def _api_words():
    """连电脑端，把库里所有标题 / 课程名 / 地点都收进来（课表词汇是封闭的，一遍就够）。"""
    import backend

    items = backend.query(range_id="all").get("items") or []
    words = []
    for it in items:
        words += [it.get("title") or "", it.get("course") or "", it.get("location") or ""]
    words = [w for w in words if w]
    print(f"从电脑端取到 {len(items)} 条，收集 {len(set(words))} 个标题/课程/地点")
    return words


def main():
    args = sys.argv[1:]
    provider = args[args.index("--provider") + 1] if "--provider" in args else ""
    extra_file = args[args.index("--extra") + 1] if "--extra" in args else ""
    force = "--force" in args

    extra = []
    if extra_file and Path(extra_file).exists():
        extra = [w.strip() for w in Path(extra_file).read_text("utf-8").splitlines() if w.strip()]
    if "--from-api" in args:
        extra += _api_words()

    todo_all = list(dict.fromkeys(items(extra) + chars(extra)))

    if "--list" in args:
        print(f"共 {len(todo_all)} 条：")
        print("\n".join(todo_all))
        return

    if provider not in ("edge", "cmd", "sapi", "minimax"):
        raise SystemExit("用法：python gen_voice.py --provider minimax|edge|sapi|cmd"
                         "（先用 --list 看看要生成什么）")
    if provider == "edge" and not shutil.which("ffmpeg"):
        raise SystemExit("[gen_voice] edge 模式需要 ffmpeg 转码：sudo apt install ffmpeg")
    if provider == "edge":
        print(f"音色：{EDGE_VOICE}  音高 {EDGE_PITCH}  语速 {EDGE_RATE}")
    if provider == "minimax":
        print(f"音色：{config.MINIMAX_VOICE_ID}（复刻）  模型 {config.MINIMAX_MODEL}")
        if not config.MINIMAX_API_KEY or not config.MINIMAX_VOICE_ID:
            raise SystemExit("[gen_voice] minimax 需要 local_secrets.py 里的 "
                             "MINIMAX_API_KEY / MINIMAX_VOICE_ID")

    config.VOICE_DIR.mkdir(parents=True, exist_ok=True)
    todo = [t for t in todo_all if force or not (config.VOICE_DIR / f"{t}.wav").exists()]
    print(f"要生成 {len(todo)}/{len(todo_all)} 条（已存在的跳过，--force 可覆盖）")
    if not todo:
        return

    if provider == "sapi":                       # 整批一次做完，不逐条起进程
        _sapi(todo, config.VOICE_DIR)
        print(f"完成。素材目录：{config.VOICE_DIR}")
        return

    fail = 0
    for i, text in enumerate(todo, 1):
        out = config.VOICE_DIR / f"{text}.wav"
        try:
            if provider == "edge":
                _edge(text, out)
            elif provider == "minimax":
                _minimax(text, out)
            else:
                _cmd(text, out)
            print(f"  [{i}/{len(todo)}] {text}")
        except Exception as e:
            fail += 1
            print(f"  [{i}/{len(todo)}] 失败 {text}: {e}")
        if provider == "edge":
            time.sleep(0.3)
        elif provider == "minimax":
            time.sleep(MINIMAX_DELAY)                # RPM 限制很紧，慢点来
    print(f"完成，失败 {fail} 条。素材目录：{config.VOICE_DIR}")


if __name__ == "__main__":
    main()
