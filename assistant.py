# -*- coding: utf-8 -*-
"""
assistant.py —— 主程序（入口）

架构：奶龙是**终端**，只负责听和说。
      唤醒 → 录音识别 → 大模型把口语修成查询参数 → 发给电脑端 → 套模板播报。
      数据（Canvas 作业 / 交我办日程 / 手动录入）全在电脑那台服务里，终端不存业务数据。

用法：
    python assistant.py                      正常语音模式
    python assistant.py --check              自检麦克风/底噪/唤醒词，不联网
    python assistant.py --system-tts         没有奶龙素材时用系统语音念回复（临时）
    python assistant.py --denoise            打开降噪（教室/宿舍这种持续底噪）
    python assistant.py --text               纯文字调试，不用麦克风
    python assistant.py --say "我今天有什么课"  跑一轮指定文本就退出
    python assistant.py --once               语音模式下只跑一轮
    python assistant.py --no-tts             只打印不发声
相关文件：stt / llm / backend / tts；配置在 config.py。
"""
import sys
import time
from datetime import datetime, timezone

import config

_args = sys.argv[1:]
if "--help" in _args or "-h" in _args:      # 别让手滑的参数把程序挂到一直等唤醒
    print(__doc__)
    raise SystemExit(0)
if "--denoise" in _args:
    config.DENOISE = True
if "--system-tts" in _args:
    config.SYSTEM_TTS = True

import backend                                                    # noqa: E402
from audio import beep, play                                      # noqa: E402  （要在改完 config 之后导入）
from llm import ConfigError, LLMError, parse                          # noqa: E402
import stt                                                        # noqa: E402
import tts                                                        # noqa: E402
from tts import TEMPLATES, cn_int, cn_text, human_time, render, speak  # noqa: E402

TEXT_MODE = "--text" in _args or "--say" in _args or "--check" in _args
ONCE = "--once" in _args
NO_TTS = "--no-tts" in _args

# 两种数据念法不一样：作业按"还剩多久"说，日程按"什么时候、在哪"说
KIND_LABEL = {"deadline": "作业", "日程": "安排", "": "安排"}
NO_ROOM = ("", "不排教室", "None")

# 交我办快照的年龄，60 秒缓存一次。查询回包里只有 Canvas 的新鲜度，
# 交我办要问 /api/status 才拿得到。
_age = {"at": 0.0, "hours": None}


def calendar_age_hours():
    """交我办快照有多少小时没更新了；没导入过或查不到返回 None。"""
    now = time.monotonic()
    if now - _age["at"] < 60:
        return _age["hours"]
    hours = None
    try:
        iso = (backend.status().get("calendar") or {}).get("imported_at")
        if iso:
            hours = (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds() / 3600
    except Exception:
        pass
    _age.update(at=now, hours=hours)
    return hours


def stale_note():
    """交我办的登录会话在电脑浏览器里，终端拿到的只是快照——旧了就主动说。

    不说的话，"你明天上午有高数"这句话从嘴里出来就等于保证，而课表可能是三天前导的。
    """
    hours = calendar_age_hours()
    if hours is None or hours < config.STALE_HOURS:
        return ""
    days = int(hours // 24)
    age = f"{cn_int(days)}天" if days else f"{cn_int(int(hours))}小时"
    return render(TEMPLATES["stale"], age=age)


def say(key_or_text, **slots):
    """按模板说话；传进来的不是模板名时当作成品文本直接说。"""
    msg = render(TEMPLATES[key_or_text], **slots) if key_or_text in TEMPLATES else key_or_text
    print("[奶龙]", msg)
    if not NO_TTS:
        speak(msg)


def handle(text):
    """一句话 → 查电脑端 → 套模板播报。"""
    cmd = parse(text)
    if config.DEBUG:                    # 调试时把 LLM 抽出来的 JSON 亮出来
        print("[llm] 意图:", cmd)

    if cmd["intent"] == "adjust":       # 想改数据：电脑端没有修改接口，老实说
        return say("write.wip")
    if cmd["intent"] != "query":        # 听不懂 / 闲聊：用固定话术，不让模型自由发挥
        return say("unknown")

    res = backend.query(kind=cmd["kind"] or None, range_id=cmd["range"] or None,
                        start=cmd["start_date"] or None, end=cmd["end_date"] or None,
                        course=cmd["course"] or None)
    if res.get("demo"):
        print("[assistant] 注意：电脑端开着演示模式，下面是编的数据")
    if res.get("warning"):
        print("[assistant] 电脑端提示:", res["warning"])

    items = res.get("items") or []
    kind = cmd["kind"]
    if not items:
        return say(render(TEMPLATES["none"], what=KIND_LABEL[kind]) + _note_if_calendar(kind, None))

    # 语音里不念整张表，只报数量 + 最急的一条，用户要细节会再问
    first = items[0]
    count = cn_int(int(res.get("count") or len(items)), liang=True)
    # 数量句跟着"问的是哪一类"走：问的就是两类时不能笼统说成"作业没交"
    prefix = {"deadline": "count.deadline", "日程": "count.schedule"}.get(kind, "count.any")
    msg = render(TEMPLATES[prefix], count=count)
    if first.get("kind") == "deadline":
        # remaining 形如"已过期 1 天"，转成中文后要把空格去掉，否则播报会一顿一顿的
        left = (first.get("remaining") or human_time(first.get("due_at")) or "").replace(" ", "")
        msg += render(TEMPLATES["urgent"], title=first["title"], remaining=cn_text(left))
    else:
        loc = first.get("location") or ""
        key = "nearest.loc" if loc not in NO_ROOM else "nearest"
        msg += render(TEMPLATES[key], title=first["title"],
                      when=human_time(first.get("start_at")), location=loc)
    return say(msg + _note_if_calendar(kind, first))


def _note_if_calendar(kind, first):
    """只有答案来自交我办（日程）时才提新鲜度。

    Canvas 是每次现抓、手动条目是本地即时写入，这两类不会因为登录态而变旧。
    """
    if kind == "deadline" or (first is not None and first.get("kind") == "deadline"):
        return ""
    return stale_note()


def ack():
    """唤醒应答：有角色原声（resources/voice/_ack.wav）就放原声，否则拼模板。"""
    if tts.has_ack_clip():
        print("[奶龙] （角色原声）")
        tts.play_ack()
    else:
        say("ack")


def turn(text=None):
    """完成一次交互；text 给定时跳过麦克风（--say 用）。"""
    if text is None:
        if TEXT_MODE:
            text = input("你> ").strip()
        else:
            ring = stt.wait_for_wake()          # 听到唤醒词就把预录音频带回来
            # 应答交给 capture 决定放不放：用户还在连着说的时候就别打断他。
            # ACK=0 直接不应答，适合一口气说完（应答音那段时间是不录音的）。
            text = stt.capture(ring, ack=ack if config.ACK else None)
    print("[我]", text or "(没听清)")
    if not text:
        return say("not_heard")
    try:
        handle(text)
    except ConfigError as e:                    # 密钥没配：直说怎么补，别说成网络问题
        print("[assistant] 配置缺失：", e)
        say("config")
    except LLMError as e:                       # 模型超时/限流：也别甩锅给电脑端
        print("[assistant]", e)
        say("llm")
    except backend.AuthError as e:
        print("[assistant]", e)
        say("auth")
    except Exception as e:                      # 超时、断网、接口报错：说一句兜底话，别崩
        print("[assistant]", e)
        say("net")


def check():
    """自检：设备 → 试说一句 → 试唤醒词 → 喇叭。不联网、不调大模型。"""
    import sounddevice as sd

    config.STT_ECHO = True                     # 自检时无论如何都显示它听成了什么
    print("=== 输入输出设备 ===")
    print(sd.query_devices())
    print(f"系统默认 输入={sd.default.device[0]} 输出={sd.default.device[1]}；"
          f"当前配置 IN_DEVICE={config.IN_DEVICE} OUT_DEVICE={config.OUT_DEVICE}")

    print("\n=== 识别引擎 ===")
    print("唤醒：Vosk 小模型（流式常驻）")
    print(f"指令：{stt.engine_label()}")
    stt.check()
    print("加载成功")

    print("\n=== 语音阈值 ===")
    print(f"VAD_THRESHOLD={config.VAD_THRESHOLD}（说话音量超过它才算开口；"
          f"老是被当成没说话就调小，被噪声打断就调大）")

    print("\n=== 麦克风电平 ===（请正常说话 2 秒）")
    mean, peak = stt.level(2.0)
    print(f"平均 {mean:.4f}　峰值 {peak:.4f}")
    if peak < 0.001:
        print("⚠️ 峰值几乎是 0：麦克风根本没收到声音。检查设备序号、是否被静音、"
              "Windows 隐私设置里是否允许应用访问麦克风。")
    elif peak < config.VAD_THRESHOLD:
        print(f"⚠️ 有声音但没超过阈值 {config.VAD_THRESHOLD}，会一直被当成没人说话。"
              f"把 VAD_THRESHOLD 调到 {peak / 2:.4f} 以下。")
    else:
        print("✅ 电平正常")

    print("\n=== 试说一句 ===（说完停一下会自动结束）")
    print("识别到：", stt.capture() or "(什么都没识别到)")

    print("\n=== 试唤醒词 ===（12 秒内对麦克风说下面任意一句）")
    for w in config.WAKE_WORDS:
        print("   ", w)
    ok = stt.wait_for_wake(sec=12)
    if ok is None:
        print("12 秒内没听到唤醒词 ❌ 看上面 [stt] 听到 实际是什么，"
              "把它加到 config.WAKE_WORDS 里（改 WAKE_WORD 环境变量也行）")
    else:
        print("唤醒成功 ✅")

    print("\n=== 喇叭 ===")
    play(beep())
    print("听到「叮」了吗？没听到就配 OUT_DEVICE")


def main():
    say_text = _args[_args.index("--say") + 1] if "--say" in _args else None

    asr = stt.engine_label()
    print(f"[assistant] 模式={'文字' if TEXT_MODE else '语音'} "
          f"降噪={'开' if config.DENOISE else '关'} "
          f"发声={'系统语音' if config.SYSTEM_TTS else ('关' if NO_TTS else '奶龙素材')} "
          f"识别={asr} 模型={config.LLM_MODEL} 电脑端={config.BACKEND_URL}")

    if not config.LLM_API_KEY:                  # 别等用户说完话才发现没配密钥
        print("[assistant] 提示：还没有 LLM_API_KEY（见 local_secrets.example.py），"
              "现在只能做 --check 这类不联网的自检")

    if "--check" in _args:                      # 只管麦克风和唤醒，不碰网络
        return check()

    if say_text is not None:                    # 单发调试，不占麦克风
        return turn(say_text)

    if not TEXT_MODE:
        stt.check()                             # 模型缺失当场报错，别等到唤醒时

    while True:
        try:
            turn()
            if ONCE:
                return
        except KeyboardInterrupt:
            print("\n[assistant] 退出")
            return
        except Exception as e:                  # 拔麦、声卡重连之类：等一等继续，不退出
            print("[assistant] 出错:", e)
            time.sleep(1)


if __name__ == "__main__":
    main()
