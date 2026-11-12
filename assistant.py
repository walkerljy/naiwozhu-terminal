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
    python assistant.py --remind             只问一次「现在该念什么」（到点提醒）
    python assistant.py --remind --preview   三个提醒位都算一遍，不管现在几点
    python assistant.py --once               语音模式下只跑一轮
    python assistant.py --no-tts             只打印不发声
相关文件：stt / llm / backend / tts；配置在 config.py。
"""
import re
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone

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

# 多轮确认的状态：删 / 隐藏 / 改期都要先说清「是这一条吗」，收到「对」才写库。
#   step = confirm（等对/不对）| pick（多条时等第几条）| time（等改成几点）
PENDING_TIMEOUT = 15
_pending = {}
_last_write = {}          # {"id": 改/删掉的那条, "extra": 改期时新增的那条}，供「撤销」
_busy = False             # 正在听 / 正在说：提醒线程要避开
_spoken = set()           # 已念过的 remind_id（内存即可，重启重念比漏掉好）
_next_poll = [0.0]        # 下一次问「现在该念什么」的时间戳


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
    """一句话 → 查电脑端 / 改数据 → 套模板播报。

    多轮确认（`_pending`）优先：上一步问过「是这一条吗」，这一句就是回答。
    """
    if _consume_pending(text):
        return

    cmd = parse(text)
    if config.DEBUG:                    # 调试时把 LLM 抽出来的 JSON 亮出来
        print("[llm] 意图:", cmd)

    if cmd["intent"] == "adjust":       # 删 / 隐藏 / 恢复 / 改期
        return _adjust(cmd)
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


# ---------------------------------------------------------------- 改 / 删 / 隐藏
# 电脑端 2026-11 上的接口（见 INTEGRATION.md）：
#   manual-* 能改能删；cal-*（交我办）不能改、只能隐藏；canvas-* 截止两种都不行。
# 删和隐藏都要确认：单轮改多轮，先复述「是这一条吗」，收到「对」才写。

def _dt(iso):
    """电脑端的时间字符串 → datetime；解析不了返回 None。"""
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _duration(item):
    a, b = _dt(item.get("start_at")), _dt(item.get("end_at"))
    return (b - a) if (a and b and b > a) else None


def _item_desc(item):
    """复述用：「明天上午八点的高等数学」。"""
    when = human_time(item.get("start_at") or item.get("due_at"))
    title = item.get("title") or ""
    return f"{when}的{title}" if when else title


def _change_when(item, change):
    """改期的目标时刻说成人话：「明天下午两点」。"""
    base = _dt(item.get("start_at")) or datetime.now()
    return human_time(f"{base.date().isoformat()} {change.get('start')}")


_CN_DIGIT = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
             "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12}


def _num(s):
    """「十五」「二十五」「9」→ 数字；认不出来返回 None。"""
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if s in _CN_DIGIT:
        return _CN_DIGIT[s]
    m = re.fullmatch(r"([一二两三四五六七八九])?十([一二两三四五六七八九])?", s)
    if m:                               # 十五 / 二十 / 四十五
        tens = _CN_DIGIT[m.group(1) or "一"]
        return tens * 10 + (_CN_DIGIT[m.group(2)] if m.group(2) else 0)
    return None


def _time_from_text(text):
    """「下午两点」「14:30」「九点半」→ "HH:MM"；认不出来返回空串。"""
    m = re.search(r"([0-9]{1,2}|[一二两三四五六七八九十]{1,3})\s*(?:点|[:：])\s*"
                  r"([0-9]{1,2}|[一二两三四五六七八九十]{1,3})?", text)
    if not m:
        return ""
    h = _num(m.group(1))
    mi = _num(m.group(2)) if m.group(2) else 0
    if mi is None:                       # 「四点半」后面的「半」不是一个数
        mi = 0
    if h is None:
        return ""
    if re.search(r"(?:点|[:：])\s*半", text):
        mi = 30
    elif re.search(r"(?:点|[:：])\s*一刻", text):
        mi = 15
    elif re.search(r"(?:点|[:：])\s*三刻", text):
        mi = 45
    if h is None or mi is None or mi > 59:
        return ""
    if "下午" in text or "晚上" in text or "傍晚" in text:
        if h < 12:
            h += 12
    elif "中午" in text and h <= 2:
        h += 12
    elif "上午" in text or "早上" in text or "凌晨" in text:
        pass
    elif 1 <= h <= 6:
        # 没说上午下午时，「四点半」在日程语境里几乎都是下午。
        # 真要说凌晨，人会说「凌晨四点」——上面那个分支已经兜住了。
        h += 12
    if 0 <= h <= 23:
        return f"{h:02d}:{mi:02d}"
    return ""


def _ordinal_of(text, n):
    """「第一条 / 第二个 / 二」→ 索引（1 起）；认不出来返回 0。"""
    m = re.search(r"第\s*([0-9一二两三四五六七八九十]{1,2})", text)
    if m:
        v = _num(m.group(1))
        if v and v <= n:
            return v
    t = text.strip()
    v = _num(t)
    if v and 1 <= v <= n:
        return v
    return 0


def _is_yes(text):
    t = text.strip()
    if any(w in t for w in ("不对", "不是", "不要", "不用", "别", "取消", "算了", "否")):
        return False
    return any(w in t for w in ("对", "是", "确认", "好", "行", "可以", "嗯", "确定",
                                "没错", "就这样", "删", "改"))


def _is_no(text):
    t = text.strip()
    return any(w in t for w in ("不对", "不是", "不要", "不用", "别", "取消", "算了", "否", "不"))


def _pending_active():
    if not _pending:
        return False
    if time.monotonic() - _pending.get("at", 0) > PENDING_TIMEOUT:
        _pending.clear()                # 15 秒没回话就当没这回事
        return False
    return True


def _listen_reply(sec=6):
    """问完确认后直接听一句（不必再喊唤醒词）。只在语音模式下有用。"""
    if TEXT_MODE:
        return ""
    try:
        return (stt.capture(ack=None, sec=sec) or "").strip()
    except Exception as e:              # 拔麦之类：当作没回答
        print("[assistant] 确认时没听清:", e)
        return ""


def _ask(action, item, change=None):
    """复述并等确认。语音模式下问完就地听一句，用户不用再喊唤醒词。"""
    _pending.update({"step": "confirm", "action": action, "items": [item],
                     "change": change, "at": time.monotonic()})
    if action == "delete":
        say("confirm.delete", what=_item_desc(item))
    else:
        say("confirm.reschedule", what=_item_desc(item), when=_change_when(item, change))
    if not TEXT_MODE:
        reply = _listen_reply()
        if reply:
            print("[我]", reply)
            _consume_pending(reply)


def _consume_pending(text):
    """把这一句当成「对 / 不对 / 第几条 / 改成几点」来处理。返回 True 表示已消费。"""
    if not _pending_active():
        return False
    text = (text or "").strip()
    step = _pending.get("step")
    action = _pending.get("action")
    items = list(_pending.get("items") or [])
    change = _pending.get("change")

    if _is_no(text):
        _pending.clear()
        say("cancelled")
        return True

    if step == "pick":
        idx = _ordinal_of(text, len(items))
        if not idx:
            if _is_yes(text):           # 只说了「对」，没说第几条：再问一次，别把选择丢掉
                _pending["at"] = time.monotonic()
                say("ask.pick")
                return True
            _pending.clear()            # 不是选项也不是应答，当新指令处理
            return False
        item = items[idx - 1]
        _pending.clear()
        _start_action(action, item, change)
        return True

    if step == "time":
        t = _time_from_text(text)
        if not t:
            _pending.clear()
            return False
        _pending.clear()
        _start_action("reschedule", items[0], {"start": t})
        return True

    if step == "confirm":
        if not _is_yes(text):
            _pending.clear()
            return False
        _pending.clear()
        _run_action(action, items[0], change)
        return True

    _pending.clear()
    return False


def _start_action(action, item, change=None):
    """走到「要动这一条」这一步：要时间就先问时间，否则直接确认。"""
    if action == "delete":
        return _ask("delete", item)
    if change and change.get("start"):
        return _ask("reschedule", item, change)
    _pending.update({"step": "time", "action": "reschedule", "items": [item],
                     "at": time.monotonic()})
    say("ask.time")
    if not TEXT_MODE:
        reply = _listen_reply()
        if reply:
            print("[我]", reply)
            _consume_pending(reply)


def _find_targets(cmd):
    """按大模型抽出来的条件找候选条目。"""
    res = backend.query(kind=cmd["kind"] or None, range_id=cmd["range"] or None,
                        start=cmd["start_date"] or None, end=cmd["end_date"] or None,
                        course=cmd["course"] or None)
    items = res.get("items") or []
    kw = cmd.get("title_kw") or ""
    if kw:
        items = [i for i in items
                 if kw in (i.get("title") or "") or kw in (i.get("course") or "")]
    if cmd.get("action") == "reschedule":       # 改期只对日程有意义
        items = [i for i in items if i.get("kind") != "deadline"]
    ordinal = cmd.get("ordinal") or 0
    if ordinal:
        items = items[ordinal - 1:ordinal]
    return items


def _adjust(cmd):
    """adjust 意图分三支：删 / 撤销 / 改期。"""
    action = cmd.get("action") or ""
    if action == "restore":
        return _do_restore()
    if not action:                      # 「作业我做完了」这类：电脑端还没有对应接口
        return say("write.wip")

    items = _find_targets(cmd)
    if not items:
        return say(render(TEMPLATES["none"], what="符合条件的条目"))

    change = {"start": cmd["start_time"], "end": cmd["end_time"]} if cmd["start_time"] else None
    if len(items) == 1:
        return _start_action(action, items[0], change)

    # 多条：念序号，让用户说「第一条」
    _pending.update({"step": "pick", "action": action, "items": items,
                     "change": change, "at": time.monotonic()})
    say("confirm.pick", n=cn_int(len(items)))
    for i, it in enumerate(items[:5], 1):
        say(f"第{cn_int(i)}条，{_item_desc(it)}。")
    if not TEXT_MODE:
        reply = _listen_reply()
        if reply:
            print("[我]", reply)
            _consume_pending(reply)


def _do_restore():
    """撤销上一步的删除 / 隐藏 / 改期。"""
    last = dict(_last_write)
    if not last.get("id"):
        return say(render(TEMPLATES["none"], what="可撤销的操作"))
    try:
        backend.item_restore(last["id"])
        if last.get("extra"):           # 改期时新增的那条，撤销时一并删掉
            try:
                backend.item_delete(last["extra"])
            except backend.ItemError:
                pass
        _last_write.clear()
        return say("ok.restore")
    except backend.ItemError as e:
        return say("fail", reason=str(e))


def _run_action(action, item, change=None):
    """确认后真正落库。"""
    try:
        if action == "delete":
            res = backend.item_delete(item["id"])
            _last_write.update({"id": item["id"]})
            return say("ok.hide" if res.get("hidden") else "ok.delete")
        return _reschedule(item, change)
    except backend.ItemError as e:
        if e.readonly:
            return say("readonly")
        return say("fail", reason=str(e))
    except backend.BackendError as e:
        print("[assistant]", e)
        return say("net")


def _reschedule(item, change):
    """改期。

    manual-* 直接 PATCH；cal-*（交我办）不能就地改，走「先加后隐」：
    顺序固定——新增成功了才隐藏原来那条，否则两条都没了。
    """
    base = _dt(item.get("start_at")) or datetime.now()
    start = f"{base.date().isoformat()}T{change['start']}"
    end = (datetime.fromisoformat(start) + (_duration(item) or timedelta(minutes=90)))
    end = end.strftime("%Y-%m-%dT%H:%M")

    if str(item["id"]).startswith("manual-"):
        backend.item_update(item["id"], {"start": start, "end": end})
        _last_write.update({"id": item["id"]})
        return say("ok.reschedule")

    add = backend.manual_add(kind="日程", title=item.get("title") or "",
                             start=start, end=end,
                             location=item.get("location") or None,
                             course=item.get("course") or None,
                             note=f"改期自 {item['id']}")
    if not add.get("ok"):
        return say("fail", reason="新的那条没加上")
    hid = backend.item_delete(item["id"])
    new_id = (add.get("item") or {}).get("id") or ""
    _last_write.update({"id": item["id"], "extra": new_id})
    tip = ((hid.get("effective") or add.get("effective") or {}).get("tip") or "")
    return say(render(TEMPLATES["ok.reschedule"]) + tip)


# ---------------------------------------------------------------- 到点提醒
# 判定全在电脑端（它知道条目、隐藏、时间窗），念的时机在这边（这边有喇叭）。
# 这边只做两件事：空闲时问一句「现在该念什么」、按 remind_id 去重。

def _hhmm(when_local, at=""):
    """提醒里只念时刻：「明天上午八点」→「上午八点」。

    走 human_time() 同一套读法（十四点 → 下午两点），再把日期那一段去掉，
    否则「明天的安排：明天上午八点…」会啰嗦一遍。
    """
    text = human_time(at) if at else ""
    m = re.search(r"(?:凌晨|早上|上午|中午|下午|晚上)?[零一二两三四五六七八九十]+点"
                  r"(?:[零一二两三四五六七八九十]+分)?", text)
    if m:
        return m.group(0)
    m = re.search(r"(\d{1,2}):(\d{2})", str(when_local or ""))
    if not m:
        return ""
    h, mi = int(m.group(1)), int(m.group(2))
    return f"{h}点" + (f"{mi}分" if mi else "")


def _remind_line(it):
    reason = it.get("reason") or ""
    title = it.get("title") or ""
    if reason == "已过期未交":
        return render(TEMPLATES["remind.item.overdue"], title=title)
    when = _hhmm(it.get("when_local"), it.get("at"))
    if it.get("kind") == "deadline":
        return render(TEMPLATES["remind.item.deadline"], when=when, title=title)
    return render(TEMPLATES["remind.item.schedule"], when=when, title=title)


_STAGE_HEAD = {"morning": "remind.morning.head",
               "evening": "remind.evening.head",
               "soon": "remind.soon.head"}


def _speak_stage(stage):
    """念一个提醒位（最多三条）。念了返回 True。"""
    items = stage.get("items") or []
    head = _STAGE_HEAD.get(stage.get("stage") or "")
    if not items or not head:
        return False
    say(head)
    for it in items[:3]:
        say(_remind_line(it))
    if stage.get("truncated"):          # 被 limit 截掉的条数，补一句
        say("remind.more", n=cn_int(int(stage.get("dropped") or 0)))
    return True


def _busy_now():
    return _busy or _pending_active()


def poll_reminders(preview=False, only_once=False):
    """空闲时问一次电脑端「现在该念什么」，按 remind_id 去重后念出来。

    失败/没到点都静默：提醒不是查询，挂了不该变成一句「出错了」。
    """
    if _busy_now():
        return
    res = backend.remind(preview=preview, limit=3)
    if not res:
        return
    for stage in res.get("stages") or []:
        rid = stage.get("remind_id") or ""
        if rid and rid in _spoken:
            continue
        if _speak_stage(stage):
            if rid:
                _spoken.add(rid)
            for it in stage.get("items") or []:
                key = it.get("remind_id") or ""
                if key:
                    _spoken.add(key)
    _prune_spoken()


def _prune_spoken():
    """只留今天的去重键，跑一周不会攒一堆。"""
    today = date.today().isoformat()
    if len(_spoken) < 100:
        return
    for key in list(_spoken):
        if today not in key:
            _spoken.discard(key)


def _reminder_loop():
    """后台轮询：空闲才问，忙就等下一轮（不消耗 180 秒的间隔）。"""
    time.sleep(10)                      # 开机先等 10 秒，别和启动自检抢
    while True:
        try:
            if time.time() >= _next_poll[0] and not _busy_now():
                poll_reminders()
                _next_poll[0] = time.time() + 180
        except Exception as e:          # 提醒线程不许把主循环带崩
            print("[assistant] 提醒轮询出错:", e)
        time.sleep(20)



def ack():
    """唤醒应答：有角色原声（resources/voice/_ack.wav）就放原声，否则拼模板。"""
    if tts.has_ack_clip():
        print("[奶龙] （角色原声）")
        tts.play_ack()
    else:
        say("ack")


def turn(text=None):
    """完成一次交互；text 给定时跳过麦克风（--say 用）。"""
    global _busy
    if text is None:
        if TEXT_MODE:
            try:
                text = input("你> ").strip()
            except EOFError:               # 管道喂输入时读到头了，当作退出，别原地打转
                raise KeyboardInterrupt
        else:
            # 等唤醒的这段时间是空闲的，提醒线程可以插话；一开口就开始忙
            ring = stt.wait_for_wake()          # 听到唤醒词就把预录音频带回来
            _busy = True
            try:
                # 应答交给 capture 决定放不放：用户还在连着说的时候就别打断他。
                # ACK=0 直接不应答，适合一口气说完（应答音那段时间是不录音的）。
                text = stt.capture(ring, ack=ack if config.ACK else None)
            finally:
                _busy = False
    print("[我]", text or "(没听清)")
    if not text:
        return say("not_heard")
    _busy = True                               # 处理 + 播报期间不许插提醒
    try:
        _respond(text)
    finally:
        _busy = False


def _respond(text):
    """把各类失败翻成人话：配置 / 模型 / 口令 / 网络各有各的说法。"""
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

    if "--remind" in _args:                     # 只问一次「现在该念什么」（调试/演示）
        # 加 --preview 就三个位都算一遍，不管现在几点
        poll_reminders(preview="--preview" in _args)
        return

    if say_text is not None:                    # 单发调试，不占麦克风
        return turn(say_text)

    if not TEXT_MODE:
        stt.check()                             # 模型缺失当场报错，别等到唤醒时
        # 到点提醒：判定在电脑端、念的时机在这边，所以是个后台轮询线程
        threading.Thread(target=_reminder_loop, daemon=True).start()

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
