# -*- coding: utf-8 -*-
"""
backend.py —— 电脑端「奶我助」服务的客户端

架构：奶龙只是个终端，只负责听和说；数据全在电脑那台 Flask 服务（队友的
`aiwozhu-app/app/server.py`）里。这个文件是终端唯一会碰网络的地方。

对接的四个接口：

    POST /api/query   {kind, range, start, end, course_kw, text}
         -> {ok, items:[...], count, kind, window, parsed, demo, warning?}

    POST /api/manual  {kind, title, due | start/end, location, course}
    DELETE /api/manual/<id>
    GET  /api/status

item 有两种，字段不一样，念之前要分开处理：

    deadline -> {kind:"deadline", id, title, course, due_at, when_local, remaining, overdue}
    日程     -> {kind:"日程",     id, title, start_at, end_at, location, when_local, remaining}

注意：**终端不传 text，传结构化参数**。队友的 query.parse 是纯规则、靠关键词
（"日程"/"课"/"要交"）判断类型，而语音识别会把"课"听成"可"、"日程"听成"日成"，
类型就判错了。终端这边用大模型把同音字纠回来、直接给 kind/range，绕开这一步。
规则解析留给网页和 CLI，那边输入是打字，没有错字问题。
"""
import json
import urllib.error
import urllib.request

import config


class BackendError(RuntimeError):
    """电脑端连不上 / 返回错误。"""


class AuthError(BackendError):
    """电脑端要求密钥，终端这边的密钥不对或没配。"""


def _call(path, body=None):
    url = config.BACKEND_URL.rstrip("/") + path
    headers = {"Content-Type": "application/json"}
    if config.BACKEND_TOKEN:
        # 电脑端从别的机器访问 /api/* 要口令；本机（127.0.0.1）一律放行。
        # 头名是 X-Access-Key，值对应电脑端的 NAIWOZHU_ACCESS_KEY。
        headers["X-Access-Key"] = config.BACKEND_TOKEN
    req = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None,
        headers=headers,
        method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=config.BACKEND_TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # 401 和 403 是两件事，分开说，否则排查时白绕一圈：
        #   401 = 口令对不上；403 = 那台机器根本没设口令，所以不对外提供数据
        if e.code == 401:
            raise AuthError("电脑端口令不对：两边的 NAIWOZHU_ACCESS_KEY 要填同一个") from None
        if e.code == 403:
            raise AuthError("电脑端还没设访问口令，所以拒绝远程访问。"
                            "先在电脑上打开设置页生成一个，再填到 BACKEND_TOKEN") from None
        detail = ""
        try:
            detail = json.loads(e.read().decode("utf-8")).get("error", "")
        except Exception:
            pass
        raise BackendError(f"电脑端返回 {e.code}：{detail}") from None
    except Exception as e:                     # 超时是 TimeoutError，不在 URLError 链上
        raise BackendError(f"连不上电脑端 {config.BACKEND_URL}：{e}") from None


def query(kind=None, range_id=None, start=None, end=None, course=None, text=None):
    """查一次。kind 是 'deadline' / '日程' / None（两类都要）。

    range_id 只认电脑端的六个预设：all / today / tomorrow / 3d / 7d / custom
    （自定义起止要同时给 start、end，格式 YYYY-MM-DD，含首尾两天）。
    """
    body = {}
    if kind:
        body["kind"] = kind
    if range_id:
        body["range"] = range_id
    if start:
        body["start"] = start
    if end:
        body["end"] = end
    if course:
        body["course_kw"] = course
    if text:
        body["text"] = text
    return _call("/api/query", body)


def manual_add(kind, title, due=None, start=None, end=None, location=None, course=None):
    """手动加一条。kind 必须显式给（电脑端会 400 拒绝猜类型）。"""
    body = {"kind": kind, "title": title}
    for k, v in (("due", due), ("start", start), ("end", end),
                 ("location", location), ("course", course)):
        if v:
            body[k] = v
    return _call("/api/manual", body)


def manual_delete(item_id):
    return _call("/api/manual/" + str(item_id))


def status():
    """电脑端自检：有没有配置 Canvas、交我办导进来几条、演示模式开没开。"""
    return _call("/api/status")
