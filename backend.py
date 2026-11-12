# -*- coding: utf-8 -*-
"""
backend.py —— 电脑端「奶我助」服务的客户端

架构：奶龙只是个终端，只负责听和说；数据全在电脑那台 Flask 服务（队友的
`aiwozhu-app/app/server.py`）里。这个文件是终端唯一会碰网络的地方。

对接的接口（2026-11 电脑端加了两批，见 INTEGRATION.md）：

    POST /api/query   {kind, range, start, end, course_kw, text}
         -> {ok, items:[...], count, kind, window, parsed, demo, warning?}

    POST /api/manual  {kind, title, due | start/end, location, course, note}
    DELETE /api/manual/<id>
    GET  /api/status

    # 条目改 / 删 / 隐藏（API.md §1.7–1.9、§2.5）
    GET    /api/items[?kind=日程][&hidden=1]
    PATCH  /api/item/<id>          {title, start, end, due, location, course, note, all_day}
    DELETE /api/item/<id>          manual-* 删除；cal-* 隐藏
    POST   /api/item/<id>/restore  撤销删除 / 撤销隐藏

    # 到点提醒（API.md §2.6）
    GET    /api/remind[?stage=&preview=1&limit=]
         -> {ok, now, stages:[{remind_id, label, items:[...]}], due_count, count, settings}

item 有两种，字段不一样，念之前要分开处理：

    deadline -> {kind:"deadline", id, title, course, due_at, when_local, remaining, overdue}
    日程     -> {kind:"日程",     id, title, start_at, end_at, location, when_local, remaining}

条目 id 前缀决定能做什么：`manual-*` 能改能删；`cal-*`（交我办）不能改、只能隐藏；
`canvas-*` 截止两种都不行（409）。写操作**一律只在本机生效**，返回体里带
`effective.tip`，播报时要用它，别把这句话写死。

注意：**终端不传 text，传结构化参数**。队友的 query.parse 是纯规则、靠关键词
（"日程"/"课"/"要交"）判断类型，而语音识别会把"课"听成"可"、"日程"听成"日成"，
类型就判错了。终端这边用大模型把同音字纠回来、直接给 kind/range，绕开这一步。
规则解析留给网页和 CLI，那边输入是打字，没有错字问题。
"""
import json
import urllib.error
import urllib.parse
import urllib.request

import config


class BackendError(RuntimeError):
    """电脑端连不上 / 返回错误。"""


class AuthError(BackendError):
    """电脑端要求密钥，终端这边的密钥不对或没配。"""


class ItemError(BackendError):
    """写条目失败。带上电脑端的状态码和 error 文本，上层按码给不同话术。

        400 字段不认识 / 改 kind / 时间解析不出来 / 标题为空
        404 条目不存在，或已经被删掉
        409 只读来源：Canvas 截止，或 cal-* 想改时间
        422 结束时间早于开始时间
    """

    def __init__(self, message, status=None, code=""):
        super().__init__(message)
        self.status = status
        self.code = code

    @property
    def readonly(self):
        """交我办课表改不了（只能隐藏后重加）。"""
        return self.status == 409 and self.code == "readonly_source"


def _call(path, body=None, method=None, timeout=None):
    """打一次电脑端。

    写接口（PATCH / DELETE / restore）失败时抛 ItemError，带上 status 和 code；
    别的失败还是 BackendError——上层对两者的处理不一样（一个要说话，一个要沉默）。
    """
    url = config.BACKEND_URL.rstrip("/") + path
    headers = {"Content-Type": "application/json"}
    if config.BACKEND_TOKEN:
        # 电脑端从别的机器访问 /api/* 要口令；本机（127.0.0.1）一律放行。
        # 头名是 X-Access-Key，值对应电脑端的 NAIWOZHU_ACCESS_KEY。
        headers["X-Access-Key"] = config.BACKEND_TOKEN
    method = method or ("POST" if body is not None else "GET")
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout or config.BACKEND_TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # 401 和 403 是两件事，分开说，否则排查时白绕一圈：
        #   401 = 口令对不上；403 = 那台机器根本没设口令，所以不对外提供数据
        if e.code == 401:
            raise AuthError("电脑端口令不对：两边的 NAIWOZHU_ACCESS_KEY 要填同一个") from None
        if e.code == 403:
            raise AuthError("电脑端还没设访问口令，所以拒绝远程访问。"
                            "先在电脑上打开设置页生成一个，再填到 BACKEND_TOKEN") from None
        detail, code = "", ""
        try:
            payload = json.loads(e.read().decode("utf-8"))
            detail = payload.get("error") or ""
            code = payload.get("code") or ""
        except Exception:
            pass
        # 写接口的 4xx 是「操作没成」，调用方要按码说不同的话；查询出错则统一兜底
        if 400 <= e.code < 500:
            raise ItemError(detail or f"电脑端返回 {e.code}", e.code, code) from None
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


def manual_add(kind, title, due=None, start=None, end=None, location=None, course=None,
               note=None, all_day=None):
    """手动加一条。kind 必须显式给（电脑端会 400 拒绝猜类型）。

    start / end 用 "YYYY-MM-DDTHH:MM"；note 可以写「改期自 <原 id>」这种备注。
    """
    body = {"kind": kind, "title": title}
    for k, v in (("due", due), ("start", start), ("end", end),
                 ("location", location), ("course", course),
                 ("note", note), ("all_day", all_day)):
        if v not in (None, ""):
            body[k] = v
    return _call("/api/manual", body)


def manual_delete(item_id):
    return _call("/api/manual/" + str(item_id), method="DELETE")


# ---------------- 条目改 / 删 / 隐藏（2026-11 电脑端新增） ----------------

def items(kind=None, hidden=False):
    """条目列表（已合并隐藏）。hidden=True 时反过来只给被隐藏的那几条。"""
    q = []
    if kind:
        q.append("kind=" + urllib.parse.quote(kind))
    if hidden:
        q.append("hidden=1")
    return _call("/api/items" + ("?" + "&".join(q) if q else ""))


def item_update(item_id, fields):
    """改一条，只有 manual-* 能改。

    fields 用短名：title / start / end / due / location / course / note / all_day。
    cal-* 会抛 ItemError(409, code="readonly_source")——改期要走「先加后隐」。
    """
    return _call("/api/item/" + str(item_id), body=fields, method="PATCH")


def item_delete(item_id):
    """删一条：manual-* 留墓碑；cal-* 写隐藏记录（交我办快照一个字不动）。"""
    return _call("/api/item/" + str(item_id), method="DELETE")


def item_restore(item_id):
    """撤销删除 / 撤销隐藏，返回里带 restored 字段。"""
    return _call("/api/item/" + str(item_id) + "/restore", body={})


# ---------------- 到点提醒 ----------------

def remind(preview=False, limit=3, stage=None):
    """现在该念什么。

    拿不到就返回 None：提醒服务挂了不该冒到播报层变成一句「出错了」。
    超时给 3 秒——它只是问一句，等待期间还得能听见唤醒词。
    """
    q = ["limit=" + str(int(limit))]
    if preview:
        q.append("preview=1")
    if stage:
        q.append("stage=" + urllib.parse.quote(stage))
    try:
        res = _call("/api/remind?" + "&".join(q), timeout=3)
    except Exception:
        return None
    if not isinstance(res, dict) or not res.get("ok"):
        return None
    return res


def status():
    """电脑端自检：有没有配置 Canvas、交我办导进来几条、演示模式开没开。"""
    return _call("/api/status")
