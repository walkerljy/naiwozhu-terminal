# -*- coding: utf-8 -*-
"""
llm.py —— 把一句话理解成结构化查询条件

职责收得很窄，只有一件事：**修语音识别的错、把口语翻译成电脑端认识的参数**。
它不查数据、不编答案——数据一律来自电脑端的 /api/query。

为什么这一步不能省：语音识别会把"课"听成"可"、"日程"听成"日成"，
而电脑端 query.parse 是按关键词判类型的纯规则，一错字类型就判错
（实测："我今天有什么可" 会连作业一起返回）。这里用大模型把同音字纠回来，
再把 kind / range 直接交过去，规则解析就只管筛选、不管猜。

主要模块：parse()。
相关文件：参数来自 config.py；被 assistant.py 调用。
"""
import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta

import config

_WEEK = "一二三四五六日"

# 提示词里用 __NOW__ / __DAYS__ 这类占位而不是 str.format，
# 因为下面示例本身就是 JSON，大括号和 format 冲突。
SYSTEM = """你是一个把中文口语转成查询参数的解析器，只输出 JSON，不要输出任何解释。
现在是 __NOW__（__WEEK__）。要查的数据在电脑端：作业截止（deadline）和日程（课表、会议、活动）。

输入来自语音识别，可能有同音错字，按语义理解，别因为错字就判成听不懂。
例如"可"多半是"课"、"日成"是"日程"、"没叫"是"没交"、"奶龙/哪种/那楼"是同一个唤醒词。

intent 只有三个：
- query    想查东西（作业、日程、课、安排、考试……）
- adjust   想改数据（改时间、改标题、加一条、删一条、推迟、标记完成）
- unknown  听不懂、或者不是来查数据的（打招呼、问天气、闲聊）

输出字段：
{"intent": "", "kind": "", "range": "", "start_date": "", "end_date": "", "course": ""}

kind   问作业/截止/要交/没交 → "deadline"；问课/日程/安排/会议/考试/活动 → "日程"；
       两类都要问、或者没说清 → ""
range  "all"(不限时间) / "today" / "tomorrow" / "3d"(今明后天) / "7d"(今天起一周)
       / "custom"(具体某几天，要同时填 start_date 和 end_date，含首尾)
course 课程简称（高数、线代、程设……），没提就空

（听不懂时不用写道歉的话，播报的话术在 tts.py 的模板里。）

未来十天的日期，直接查表，不要自己算：
__DAYS__

示例：
用户"我还有哪些作业没交" → {"intent":"query","kind":"deadline","range":"all","start_date":"","end_date":"","course":""}
用户"我今天有什么课" → {"intent":"query","kind":"日程","range":"today","start_date":"","end_date":"","course":""}
用户"明天有啥要交的" → {"intent":"query","kind":"deadline","range":"tomorrow","start_date":"","end_date":"","course":""}
用户"这周有什么安排" → {"intent":"query","kind":"","range":"7d","start_date":"","end_date":"","course":""}
用户"高数有什么作业" → {"intent":"query","kind":"deadline","range":"all","start_date":"","end_date":"","course":"高数"}
用户"下周三有什么" → {"intent":"query","kind":"","range":"custom","start_date":"__NEXTWED__","end_date":"__NEXTWED__","course":""}
用户"把明天第一节改到下午" → {"intent":"adjust","kind":"","range":"","start_date":"","end_date":"","course":""}
用户"作业我做完了" → {"intent":"adjust","kind":"","range":"","start_date":"","end_date":"","course":""}
用户"今天天气不错" → {"intent":"unknown","kind":"","range":"","start_date":"","end_date":"","course":""}

只输出 JSON。"""

_RANGES = ("all", "today", "tomorrow", "3d", "7d", "custom")
_KINDS = ("deadline", "日程")


class ConfigError(RuntimeError):
    """本地配置缺失（比如还没填 LLM_API_KEY）。

    单独一个类型是为了和网络错误分开：密钥没配的情况下请求会返回 401，
    上层如果按"网络不通"兜底，就会说成"连不上电脑那边"——完全指错方向。
    """


class LLMError(RuntimeError):
    """模型这边的问题（超时、断网、限流、回包不是 JSON）。

    和 backend 的错误分开：两者都归成"网络不通"时，用户会去查错的那一头。
    """


def _system():
    now = datetime.now()
    today = now.date()
    labels = ("今天", "明天", "后天")
    days = "；".join(f"{labels[n]}={(today + timedelta(days=n)).isoformat()}" for n in range(3))
    days += "；" + "；".join(
        f"{(today + timedelta(days=n)).isoformat()}=周{_WEEK[(today + timedelta(days=n)).weekday()]}"
        for n in range(3, 10))
    # "下周三"：先把"下周一"算出来，再加两天
    next_monday = today + timedelta(days=(7 - today.weekday()) % 7 or 7)
    return (SYSTEM
            .replace("__NOW__", now.strftime("%Y-%m-%d %H:%M"))
            .replace("__WEEK__", "星期" + _WEEK[now.weekday()])
            .replace("__DAYS__", days)
            .replace("__NEXTWED__", (next_monday + timedelta(days=2)).isoformat()))


def _post(body, allow_retry=True):
    req = urllib.request.Request(
        config.LLM_BASE_URL + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + config.LLM_API_KEY})
    try:
        with urllib.request.urlopen(req, timeout=config.LLM_TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8"))["choices"][0]["message"]["content"]
    except urllib.error.HTTPError as e:
        if e.code == 429 and allow_retry:     # 校内网关有速率限制，退一步重试一次
            time.sleep(1.5)
            return _post(body, allow_retry=False)
        raise LLMError(f"模型接口返回 {e.code}：{e.read().decode('utf-8', 'ignore')[:120]}") from None
    except Exception as e:
        # 超时抛的是 TimeoutError、断网是 URLError、DNS 失败是 gaierror——
        # 它们不在同一个继承链上，逐个列会漏，这里统一归成一句话。
        raise LLMError(f"模型接口不可用：{e}") from None


def parse(text):
    """返回统一格式的 dict；模型输出畸形时重试一次，还不行就抛异常。"""
    if not config.LLM_API_KEY:
        # 先说清楚是配置问题，别让 401 变成"网络不通"
        raise ConfigError(
            "没有 LLM_API_KEY：把 local_secrets.example.py 复制成 local_secrets.py 并填上，"
            "或者设环境变量 LLM_API_KEY")
    body = {
        "model": config.LLM_MODEL,
        "messages": [{"role": "system", "content": _system()},
                     {"role": "user", "content": text}],
        "temperature": 0,
        "max_tokens": 200,
        "response_format": {"type": "json_object"},   # 服务端已实测支持
    }
    content = _post(body)
    try:
        return _clean(json.loads(content))
    except json.JSONDecodeError:
        body["messages"] += [
            {"role": "assistant", "content": content},
            {"role": "user", "content": "上面不是合法 JSON，请只输出一个 JSON 对象。"},
        ]
        try:
            return _clean(json.loads(_post(body)))
        except json.JSONDecodeError:
            raise LLMError("模型两次都回了非 JSON，先看看接口是不是换了模型") from None


def _date_ok(s):
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return s
    except (TypeError, ValueError):
        return ""


def _clean(d):
    """把模型输出收进电脑端认识的取值，非法值一律降级。"""
    intent = str(d.get("intent") or "unknown").strip().lower()
    if intent not in ("query", "adjust", "unknown"):
        intent = "unknown"
    kind = str(d.get("kind") or "").strip()
    if kind not in _KINDS:
        kind = ""
    rng = str(d.get("range") or "").strip().lower()
    if rng not in _RANGES:
        rng = "all" if intent == "query" else ""
    start = _date_ok(str(d.get("start_date") or "").strip())
    end = _date_ok(str(d.get("end_date") or "").strip())
    if rng == "custom" and not (start and end):
        rng = "all"                     # 说了自定义却没给出日期，退回不限时间而不是报错
    return {
        "intent": intent,
        "kind": kind,
        "range": rng,
        "start_date": start if rng == "custom" else "",
        "end_date": end if rng == "custom" else "",
        "course": str(d.get("course") or "").strip(),
    }
