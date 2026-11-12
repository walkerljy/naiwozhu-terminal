# 两端对接说明（奶龙终端 ↔ 电脑端）

## 一句话架构

**奶龙只负责"听"和"说"；数据、判断、写库全在电脑端。**

```
   树莓派 / Windows（本项目）              PC（aiwozhu-app）
  ┌───────────────────────┐        ┌──────────────────────────────┐
  麦克风 →│ 唤醒词（Vosk 离线）     │        │ app/server.py (Flask)         │
  │ 录音断句 → 本地转写     │        │  ├ POST /api/query   查数据   │
  │ 大模型只做"纠错+转参数" ─┼─ HTTP ─→│  ├ POST /api/manual  手动录入 │
  │ 模板拼接 ←───────────────┼────────│  └ GET  /api/status  自检     │
  └───────────────────────┘        │ 数据源：Canvas+交我办+手动    │
       扬声器 ←──────────────────────└──────────────────────────────┘
```

## 为什么这么切

1. **数据必须在电脑端**：交我办的会话在浏览器里，只有 PC 有；Canvas 令牌也在 PC 上。
2. **唤醒和识别必须在终端**：否则每句话都要把音频传过去，延迟和体感都不行。
3. **大模型只放在终端，且只干一件事**：把语音识别结果修正成参数，不碰数据、不编答案。

## 已经跑通的契约

终端侧代码在 `backend.py`，实测走的是演示数据。

```
POST /api/query
    入参 {kind?, range?, start?, end?, course_kw?, text?}
    出参 {ok, items[], count, kind, window, parsed?, demo, warning?}

POST /api/manual       {kind, title, due | start/end, location, course, note}
DELETE /api/manual/<id>
GET  /api/status

# 2026-11 电脑端新增（API.md §1.7–1.9、§2.5、§2.6）
GET    /api/items[?kind=日程][&hidden=1]
PATCH  /api/item/<id>          {title, start, end, due, location, course, note, all_day}
DELETE /api/item/<id>          manual-* 删除；cal-* 隐藏；canvas-* 409
POST   /api/item/<id>/restore  撤销删除 / 撤销隐藏
GET    /api/remind[?stage=&preview=1&limit=]   现在该念什么
```

`kind` 只有两个取值：`deadline`（作业截止）/ `日程`。
`range` 只有六个：`all / today / tomorrow / 3d / 7d / custom`（custom 要配 start、end，含首尾两天）。

条目形状（终端按 `kind` 分开念）：

```
deadline  {kind:"deadline", id, title, course, due_at, when_local, remaining, overdue, editable, hideable}
日程       {kind:"日程",     id, title, start_at, end_at, location, when_local, remaining, editable, hideable}
```

`editable` / `hideable` 按 id 前缀算：`manual-*` 可改可删，`cal-*`（交我办）只能隐藏，
`canvas-*` 截止两种都不行。

### 写条目：只在本机生效

`PATCH` / `DELETE` / `restore` 都只动本机数据（交我办快照、Canvas 一个字都不改），
返回体里有 `effective.local_only=true` 和 `effective.tip`。播报时要带上那句 tip，
**别把「只在本机生效」写死在话术里**，否则文案一改两边就对不上。

| 码 | 含义 | 终端现在的说法 |
|---|---|---|
| 400 | 字段不认识 / 改 kind / 时间解析不出来 / 标题为空 | 「没办成，<服务端的 error>」 |
| 404 | 条目不存在或已删 | 同上（服务端给的是人话） |
| 409 `readonly_source` | `cal-*` 想就地改时间 | 「交我办里的课表改不了…」，另走「先加后隐」 |
| 409 其它 | Canvas 截止改/删 | 直接念服务端的 error |
| 422 | 结束早于开始 | 「没办成，结束时间比开始时间早」 |

### 改交我办条目的时间：先加后隐

`cal-*` 不能就地改（本地改课表会造出一份假课表）。终端的两步：

1. `POST /api/manual` 新增一条日程，`note` 写 `改期自 <原 id>`；
2. 成功后 `DELETE /api/item/<原 id>` 隐藏原来那条。

**顺序固定：先加后隐**——新增失败就别隐藏，免得两条都没了。`manual-*` 的条目才可以直接 `PATCH`。

### 到点提醒：判定在电脑端、时机在终端

`GET /api/remind` 回答「现在该念什么」，**不发任何网络请求**（Canvas 用缓存），
早上八点还没连上网时照样答得出来。

- 三个位：`morning`（08:00–12:00 今日 + 今日截止 + 已过期未交）、
  `evening`（22:00–次日 02:00 明日）、`soon`（日程开始前 60 分钟内）。
- `stages` 为空 = 现在没到点，**终端什么都不说**，不是「出错了」。
- `remind_id` 是去重键（整段一个、每条一个），终端记在内存里。
- 终端实现：`backend.remind()`（超时 3 秒、失败返回 None）+ `assistant._reminder_loop()`
  后台线程（开机等 10 秒，之后每 180 秒在空闲时问一次；正在听 / 正在播报 / 正在多轮确认时跳过）。
- 一个位最多念 3 条，`truncated` 为真时补一句「还有 N 条，问我」。
- 时刻只念时分（`_hhmm()` 走 `human_time()` 同一套读法），不念日期、不念字段名。

## 终端为什么传结构化参数，而不是把 text 丢过去

`query.parse()` 是纯规则、按关键词判类型，而语音识别会出错字。实测（演示数据）：

| 终端发过去的话 | 电脑端自己解析的结果 |
|---|---|
| 我今天有什么**课** | kind=日程，1 条 ✓ |
| 我今天有什么**可**（"课"听错） | kind=None，3 条 ✗ 把作业也混进来了 |
| 我明天有什么**日程** | kind=日程，2 条 ✓ |
| 我明天有什么**日成**（听错） | kind=None，4 条 ✗ |

所以终端先用大模型把同音字纠回来、直接给 `kind` + `range`，**绕开关键词那一层**；
`text` 入口原样保留给网页和 CLI（那边是打字，没有错字问题）。

## 电脑端已经做完的事

2026-10-22 那次更新（+3114 行、新增 `API.md` / `SYNC.md`）把我提的三点做了两个半：

| 项 | 现在的做法 |
|---|---|
| 监听地址 | `--host`（env `NAIWOZHU_HOST`），默认仍是 `127.0.0.1` |
| 访问口令 | `--access-key`（env `NAIWOZHU_ACCESS_KEY`）；头名 **`X-Access-Key`**，也认 `?key=` 和 `naiwozhu_key` cookie |
| 修改接口 | 2026-11 补上了：`PATCH /api/item/<id>`、`DELETE`、`restore`（见上） |

三条安全设计值得记一下：

- **本机（127.0.0.1）一律放行**，单机用时不用输口令。
- **绑 `0.0.0.0` 却没设口令 → 直接拒绝启动**（实测：打印原因并 `return 2`）。
- 状态码分得清：**401 = 口令不对**，**403 = 那台机器根本没设口令**。

终端侧已经对齐（`backend.py` 发 `X-Access-Key`，401/403 分别报不同的错）。

### 实测（本机当"树莓派"去连局域网地址）

| 情况 | 结果 |
|---|---|
| 不带口令 | `401 访问口令不对` |
| 口令写错 | `401` |
| 口令正确 | `200` |
| 用 `?key=` 传 | `200` |

### 一个坑：`--access-key` 会写盘

它会把口令存进 `app/config.local.json`（不是只用这一次）。我测试时就被这一点绊了一下：
之后再起一个不带口令的 `0.0.0.0` 实例，它从配置里读到了测试口令，于是照样启动。
**用完记得把配置里那行清掉**，或者干脆用环境变量 `NAIWOZHU_ACCESS_KEY`。

## 这次升级带来的两处破坏性变化

`SYNC.md` 第 9 节说"对外只加不改"，但确实有两处语义变了：

1. **手动条目 id**：`manual-3` → `manual-<设备号>-3`（实测 `manual-c81c0f1b-3`）。
   原因是两台机器各自的第一条都叫 `manual-1`，同步时会互相吞掉。
2. **`app/data/*.json` 不再存** `remaining` / `overdue` / `when_local`，读取时按当前时间现算。

对终端**没有影响**（id 一律用服务端返回的），但以前贴在终端里的
`curl -X DELETE http://…/api/manual/manual-1` 已经失效了，要换成新格式的 id。

新增的 `POST /api/sync`、`/api/sync/now`、`/api/sync/settings` 是两台机器互推数据用的，
终端不碰。

## 现在要定的一件事：树莓派跑不跑服务

他们的 `SYNC.md` 假设**树莓派上也跑一份 `app/server.py`**（作为第二个数据节点），
这和我原来的设计（树莓派只跑语音终端）不是一回事：

| | 方案 A：瘦终端 | 方案 B：双节点（`SYNC.md` 的设计） |
|---|---|---|
| 树莓派上跑什么 | 只有语音程序 | `app/server.py` + 同步线程 + 语音程序 |
| 语音连谁 | 电脑的 `192.168.x.x:5173` | 本机 `127.0.0.1:5173` |
| 要不要配口令 | 要（跨机器走 `/api/*`） | **不用**（本机放行） |
| 电脑关机时 | 问答不了 | **照样能答**（用同步过来的缓存） |
| 树莓派负担 | 轻 | 多一个 Flask（内存 ~50MB） |
| 依赖 | 网络要通 | 要先同步一次，之后离线也能用 |

**建议 B**：既然他们已经把同步做完了，语音那条路走本机就等于免费绕开了鉴权，
而且答辩现场电脑不一定开着。代价只是树莓派多跑一个进程。

如果选 A，就把电脑端起成：
`.\app\run.ps1 -BindHost 0.0.0.0 -AccessKey '你的口令'`，
终端侧 `BACKEND_URL=http://<电脑IP>:5173`、`BACKEND_TOKEN='同一个口令'`。

## 终端这边已经改完的

| 改动 | 说明 |
|---|---|
| `jiaowoban.py` 已删除 | 不再直接抓交我办（Cookie 那套交给电脑端的浏览器扩展） |
| 新增 `backend.py` | 终端唯一碰网络的地方；口令头已对齐 `X-Access-Key` |
| `llm.py` 收窄 | 只输出结构化参数（`kind/range/日期/course` + `action/title_kw/ordinal/start_time`），不猜数据 |
| `tts.py` 模板重写 | 按 deadline / 日程 两套念法 + 唤醒应答"我在" + 确认/结果/提醒话术 |
| `gen_voice.py` | 新增 `--provider sapi`（Windows 自带语音先跑出声）、`--from-api` 向电脑端取词、缺字自愈 |
| `stt.py` | 唤醒支持多别名 + 同音字族正则；实时打印识别结果；`capture(sec=)` 支持短听 |
| 写操作（2026-11） | 多轮确认后删/隐藏/恢复/改期；`cal-*` 走「先加后隐」；错误按 400/404/409/422 分别说话 |
| 到点提醒（2026-11） | `--remind [--preview]` 可单独跑；正式运行时后台线程空闲轮询，按 `remind_id` 去重 |

## 已知的小坑

- `is_query=false` 时 `/api/query` 的回包**没有 `count` 字段**（只有 `note`）。
  终端不传 `text`，碰不到这条分支；但网页/CLI 侧用 `count` 前要先判一下。
- **演示模式是服务端全局开关**，忘了关就会一直念假数据。终端检测到 `demo=true` 会在
  控制台打一行提醒，但不会拦着不说。当前他们库里这个开关是**开着**的。
- Canvas 没配令牌时回包带 `warning`，终端打在控制台、不念出来（念出来太吵）。
- `app/data/` 里的 `remaining` 等派生字段没了，手翻文件会以为缺字段，走接口一切正常。
- **`cal-*` 的 id 会漂移**：交我办事件缺 `eventId` 时 id 由「标题|开始|结束」拼成，
  上游一改时间 id 就变，之前的隐藏记录会失效。`/api/status` 的 `orphan_hides` 能看到。
- **`PATCH` 改时间要 start/end 一起给**：只给 `start` 而 `end` 还是旧的，
  服务端会判「结束早于开始」回 422。终端改期时按原时长自动算 `end`。
- 到点提醒的空 `stages` 是「没到点」，**别念成「提醒服务异常」**；连不上也静默跳过。

