# clip2table v2 —— 任何爬不动的数据，用真浏览器/真程序复制成表

> **历史材料，不代表当前能力。**
> 本文件记录的是当时的方案 / 实验 / 验收过程，保留是为了追溯"为什么变成现在这样"。
> 想知道这个工具现在能做什么、怎么用，请看 `docs/usage.md`；想知道代码怎么组织，看 `docs/architecture.md`。
> 文中出现的命令、参数、文件名可能已经改名或废弃，**不要照抄**。

## 0. 结论先行

| 决定 | 内容 |
|---|---|
| 一句话 | 对**任何你想拿的数据**（尤其是爬不动的网站），Python 自动发 Ctrl+A / Ctrl+C，把复制出来的内容变成表，再自动翻页 |
| 目标场景 | 难爬站点：登录态、JS 渲染、无限滚动、指纹与频控拦截；以及桌面客户端、内网系统、PDF 阅读器、Excel |
| 数据范围 | **不限行业、不限站点、不限数据类型**：商品、订单、招聘、房源、企业名录、政务公开、文献列表、监控台账、后台导出……凡是屏幕上能选中复制的，都能进表 |
| 关键事实 | **Python 从不直接请求目标站**，也**不需要人手动复制**。请求由用户的真浏览器发出，复制由脚本发真实按键完成 |
| 核心动作 | 自动全选 → 自动复制 → 读剪贴板 → 抽表 → 自动翻页。一轮 = 一页 |
| 通道 | 直连 HTTP ｜ CDP 驱动已登录浏览器 ｜ Win32 键盘注入任意窗口。三通道输出同一个 `Captured` 对象，进同一条流水线 |
| 人的位置 | 只在三件事上出现：**登录、过验证码、确认第一页对不对**。其余全自动 |
| 扩展 | 站点卡（TOML）：一种版式 = 一份卡，加覆盖面不改代码 |
| 首跑 | `infer` 从抓到的第一坨文本直接出表，用户不必先懂卡 |
| 累积 | 一次命令翻 N 页，去重累积进同一张表；断点可续 |
| 依赖 | 零必需依赖。xlsx 写出、CDP 客户端、键盘注入均自实现 |
| 形态 | CLI + SKILL.md（agent 的决策循环，不是参数手册） |
| 判据 | 见 §2.1：**对手升级反爬时，本方案不失效**——因为它不发自动化请求 |

**这就是实质**：绝大多数爬虫死在「请求不像人」。本方案不解决「怎么把请求伪装成人」——它绕开整个问题：**让真的浏览器去请求，让 Python 自动按键复制，只做搬运和结构化**。所以它天然联网、天然带登录态、天然有真实指纹，因为它就是用户自己在浏览。

---

## 1. 做什么、不做什么

| 是 | 不是 |
|---|---|
| **难爬站点**：登录态、JS 渲染、无限滚动、频控与指纹拦截的后台 | 通用爬虫调度框架 |
| 复用**你自己的浏览器会话**：真实 IP、真实 UA、真实 Cookie、真实指纹 | 伪造 UA / 代理池 / 浏览器指纹伪装 |
| 驱动浏览器**自动全选 → 自动复制 → 自动翻页**，一轮一页 | 破解验证码、绕过人机校验、撞频控阈值 |
| 桌面软件、PDF、内网系统里能选中的文字（复制即可入表） | 图片 OCR（OCR 在它上游，本工具接文本） |
| 网页表格、商品/评论/订单/名录/文献等任意列表页、公开 JSON 接口 | 数据清洗与建模（导出即交付） |

边界只有一条：**人能看见、能选中、能复制的东西，本工具都能进表**——无论它在哪个站点、哪个软件里、是什么行业的数据。

**它不认识业务**：它不知道「评论」「商品」「房源」「招聘」有什么区别。它只认识「一条记录长什么样」和「字段在哪一行」。所以它天生不挑数据——今天抓电商后台订单，明天抓政务公开名录，同一套代码、换一张卡。

**同一套代码能抓什么（这张表只是举例，不是范围）**

| 领域 | 典型数据 |
|---|---|
| 电商 | 商品列表、SKU 与价格、订单、库存、竞品价格监控、评价 |
| 招聘 | 职位列表、薪资、公司信息、投递记录 |
| 房产 | 房源、成交价、租赁挂牌 |
| 企业与工商 | 企业名录、股东、招投标、专利 |
| 政务 | 公开公示、处罚决定、政策文件目录 |
| 学术 | 文献列表、引用、基金项目 |
| 金融 | 行情快照、公告、财报科目 |
| 运营 | 后台报表、广告投放数据、客服工单 |
| 非网页 | 桌面客户端里的任何列表、PDF 目录、内网 OA 台账、Excel 里没有导出入口的表 |

**判断标准只有一条**：这东西是不是「你在屏幕上能看见、能选中、能复制」的文本。是 → 能抓。不是（比如只是张图）→ 先 OCR，再进这个工具。

**不越的那条线**：本工具**不冒充**任何人。它用你的真实浏览器、在你已登录的会话里，做你手本来就要做的动作（全选、复制、下一页），只是把这个动作自动化并把结果结构化。所以它不碰验证码、不碰指纹伪装、不碰请求伪造——因为**根本不需要**：请求本来就是你浏览器发的。

---

## 2. 核心机制：把「复制粘贴」全自动化成采集流水线

### 2.1 为什么这条路能通

所有爬虫方案都在回答同一个问题：**怎么让请求看起来像人**。答案越堆越复杂——UA 池、代理、指纹补丁、鼠标轨迹、TLS 指纹对齐，而对面每一轮升级都要重新打一次。

本方案不回答这个问题，它**取消**了这个问题：

| | 常规爬虫 | 本方案 |
|---|---|---|
| 谁发请求 | Python（需要伪装成人） | **用户自己的浏览器**（本来就是人） |
| 指纹 | 需对齐、需维护 | 无需对齐，就是真的 |
| 登录态 | 需注入 Cookie / 存密码 | 用户的会话，已经在登录状态 |
| 频控 | 会被判机器 | 请求节奏由人配的 `pause_ms` 决定，且**请求是本机浏览器发的**，频控只能看到正常浏览 |
| 对面升级反爬 | 方案可能整体失效 | **不受影响**——反爬拦的是脚本发出的请求，而这里请求由浏览器发出；脚本只做按键和搬运 |

**这是唯一的护城河**：不管站点怎么升级反爬，只要数据还在页面上、还能被选中复制，本工具就能拿到。反爬对抗的是「自动化请求」，而这里根本没有自动化请求——请求是浏览器自己发的，脚本只负责按 Ctrl+A / Ctrl+C 和翻页。

### 2.2 一轮采集的完整动作（全自动，无人介入）

```
① 聚焦目标   找到目标窗口/标签页（浏览器按 URL 匹配，桌面程序按窗口标题/进程名）
② 展开准备   注入 JS（浏览器）或发按键（桌面程序）：点开筛选/「加载更多」→ 滚动到底
③ 自动选全   发 Ctrl+A（浏览器用 CDP 真实按键；桌面程序用 Win32 SendInput）
④ 自动复制   发 Ctrl+C，等剪贴板序列号变化（不是 sleep 猜）
⑤ 读剪贴板   Win32 读 CF_HTML（带结构）+ 纯文本（兜底）+ TSV
⑥ 抽表       sniff → normalize → segment → extract → assemble
⑦ 累积去重   入 dataset，state 记记录 hash
⑧ 自动翻页   点「下一页」/ 发 PageDown / 滚动触发加载 → 回到 ③，直到 max_pages 或「下一页」消失
```

**③④ 是整个项目的技术心脏**，而且必须是脚本按键，不是人按键。它有两个引擎，覆盖不同目标：

| 引擎 | 怎么按 | 覆盖 | 前台占用 |
|---|---|---|---|
| **E1 CDP 按键** | `Input.dispatchKeyEvent` 发 keyDown/keyUp + `modifiers=2`（Ctrl）+ `windowsVirtualKeyCode=67` | 浏览器内的任何页面（含跨域 iframe，因为作用于渲染进程） | 窗口只是被盖住时不动前台；最小化时要短暂借前台并归还 |
| **E2 Win32 键盘注入** | `SetForegroundWindow` 聚焦 → `SendInput` 发 Ctrl+A / Ctrl+C / PageDown | **任何 Windows 程序**：桌面客户端、PDF 阅读器、内网系统、Excel、终端 | ❌ 会占用前台，必须独占 |

**E1 是浏览器场景的正解**：CDP 的按键事件直接进渲染进程，浏览器自己执行 `document.execCommand('copy')` 的等价路径，写入系统剪贴板。不依赖 `navigator.clipboard` 权限、不受「非用户手势不能复制」限制——因为对浏览器来说这就是一次真的按键。

**前台到底会不会被动**，实测比设计时以为的更细（`_probe/m2_4_ab_setbounds.py`、`m2_4_fallback_ab.py`、`m2_4_cli_acceptance.py`）：按键能进后台标签页，但 **Chromium 拒绝为隐藏标签页写剪贴板**，所以复制的前提是标签页 `visible`。窗口只是被别的窗口盖住时，`Page.bringToFront` 能在不动 OS 前台的前提下让它 visible（8/8）；浏览器窗口**最小化**时，同一调用会还原窗口并抢走前台（5/5 归还成功）；可见性修不回来时，发键之前就报错退出。

**E2 是「任何地方」的正解**：它完全是操作系统级输入，目标程序分不清是脚本还是人——因为走的确实是同一个 `SendInput` 通道，和人按键盘没有区别。这条引擎让「不拘泥于网站」从口号变成事实：**同一套卡、同一套流水线，能抓浏览器、能抓微信读书客户端、能抓内网 OA、能抓只有 PDF 阅读器能打开的文件**。

**为什么不用 UIA 直接读控件文本**：UIA 能读到，但读出来的是控件树碎片，字段边界要重新猜一遍；而且很多程序（尤其自绘 UI、Electron、游戏化界面）UIA 树是空的。**复制得到的是「用户看见的那段文本」，边界天然正确**——这条判断适用于 E1 和 E2 两个引擎。

**关键取舍**：全自动复制而不是读 DOM，是刻意的。读 DOM 要写选择器、跟着改版维护；**自动复制不需要任何选择器**——用户看见什么，复制出来就是什么。选择器只在需要精确字段时才用（卡里的 `dom` 原语）。这把「加一个网站」的成本从「分析 DOM 结构」降到「什么都不用做」。

**同步点必须是剪贴板序列号，不是 sleep**：`GetClipboardSequenceNumber()` 轮询到变化才读，超时（默认 3000ms）才报错。用 `time.sleep(2)` 猜的脚本在慢机器上必然读到上一页的旧数据——**这是最脏的一类 bug：不报错，但数据全错位**。

### 2.3 三条通道的分工

| 通道 | 什么时候用它 | 拿到什么 | 失败退路 |
|---|---|---|---|
| **A 直连** | 数据在 HTML 里或有公开 JSON；无登录、无 JS 渲染 | 原始 HTML / JSON（最快，毫秒级） | 401/403、只有骨架 → B |
| **B 浏览器** | 需登录态、JS 渲染、无限滚动、要先点筛选开关、**难爬站点的正解** | CDP 按键复制出的 CF_HTML，或渲染后 DOM | 无调试端口 → C |
| **C 桌面** | 任何 Windows 程序：桌面客户端、PDF、内网系统、Excel、浏览器（作为 B 的降级） | Win32 按键复制出的 纯文本 / CF_HTML / RTF / TSV | 无，终局通道 |

**通道选择规则（CLI 与 SKILL.md 共用这一张表，不许各写一套）**

1. 卡里写了 `channel` → 用它，不再判断。
2. 没写 → 试 A，2 秒超时。HTTP 200 且命中目标结构 → A。
3. 401/403 / 需要 Cookie / HTML 只有空壳 → B。
4. B 不可用（无调试端口）但目标在浏览器里 → C + E2（Win32 注入浏览器窗口）。
5. 目标不是浏览器 → 直接 C + E2。

**C 不是兜底，是覆盖面**：A 和 B 都要求「目标是浏览器且可编程」，而现实里大量数据在非浏览器程序里。C + E2 是唯一能覆盖「任何 Windows 窗口」的通道，而且它和 B 用的是同一套 `[harvest]` 配置——**只换引擎，不换卡**。

无论走哪条，**报告第一行必须写**：本次通道 + 引擎（E1/E2/HTTP）+ 判定依据。用户要知道自己在信任什么。


---

## 3. 流水线：六个环节

三通道汇聚后的唯一路径。每个环节职责单一、可单独重跑。

| # | 环节 | 职责 | 硬约束 |
|---|---|---|---|
| 1 | **sniff** | 判定输入形态：HTML 表格 / TSV / JSON / Markdown 表 / 纯文本 | 判定结果写进报告，不许黑箱 |
| 2 | **normalize** | 裸 CR → LF、NBSP、零宽字符、行尾空白、连续空行；可逐项关闭 | 归一后行数记入报告；原始文本原样保留供回溯 |
| 3 | **segment** | 切记录：谁是一条记录的起点与终点 | 边界用**两行联合证据**判定（见 §4）。单行长度阈值已被实测证伪，不再作为方案 |
| 4 | **extract** | 每字段一条规则（`dom`/`cell`/`line`/`search`/`span`/`json`/`const`） | 每个值带来源行号；不命中就是空 |
| 5 | **assemble** | 列顺序、类型转换、必填检查 | **绝不合成值**：没有就留空并标 `needs_review`，不填「未知」「无」 |
| 6 | **export** | csv / md / xlsx / jsonl，一次可出多种 | 大表优先 csv/jsonl；xlsx 单格 32767 字符上限在写出时检查 |

**兜底规则**：未被任何记录覆盖的行进 `unparsed.txt`（带行号），报告置顶显示条数。这是唯一允许的「丢弃」——它没有丢，只是没进表。

**为什么把 segment 与 extract 分成两级**：字段抽错只丢一个格子，边界切错会让整条记录的所有字段错位，并污染相邻记录。架构预算必须不对称地压在 segment 上。

---

## 4. 站点卡

一份卡 = 一种版式。它是**数据**，不是代码：加覆盖面 = 加一个 TOML 文件。

### 4.1 顶层键

| 键 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `schema` | int | ✓ | 恒为 1 |
| `name` | str | ✓ | `^[a-z0-9][a-z0-9-]*$` |
| `channel` | `http`｜`browser`｜`desktop` | | 缺省则按 §2.3 自动判断 |
| `[harvest]` | table | B/C 需要 | **自动复制策略**：谁按 Ctrl+A / Ctrl+C、怎么等 |
| `[target]` | table | C 需要 | 怎么找到那个窗口/进程/标签页 |
| `[fetch]` | table | A/B 需要 | 打开哪个页、等什么、点开什么、滚多久 |
| `[segment]` | table | ✓ | 记录边界 |
| `[fields.*]` | table | ✓ ≥1 | 字段规则，键名即列名（保序） |
| `[paging]` | table | | 翻页（`--max-pages` 用） |
| `[dedupe]` | table | | 去重键与冲突策略 |
| `samples` | str | | 卡自带样本目录，`test` 用 |

### 4.2 例一：难爬站点——自动复制模式（不写任何选择器）

这是本项目的**主打形态**。整张卡没有一条 DOM 选择器，因为它不读 DOM，它让浏览器自己复制。

```toml
schema  = 1
name    = "some-hard-site"
channel = "browser"

[fetch]
url     = "https://example.com/list?page=1"
ready   = "load"                       # 等 load 即可，不需要等某个选择器
expand  = ["a[data-filter='all']"]     # 可选：先点开「显示全部」
scroll  = { until = "stable", max = 40, pause_ms = 600 }

[harvest]
mode       = "copy"                    # copy = 全选 + 真实 Ctrl+C
select_all = true                      # 先 Ctrl+A
select_in  = "main"                    # 可选：只选某个容器（CSS），避免选中导航栏
timeout_ms = 3000                      # 等剪贴板序列号变化

[paging]
style      = "click"
next       = "a.next"                  # 点它翻页；消失即停
max_pages  = 50
pause_ms   = 1200                      # 节流，按真实人手速

[segment]
mode        = "anchor"
anchor      = '^(?!\s).{1,24}$'
anchor_next = '^\d{4}\s*[-/.年]\s*\d{1,2}'

[fields.raw]
kind  = "line"
index = 0

[dedupe]
key          = ["raw"]
on_collision = "keep_first"
```

### 4.3 例二：任何 Windows 程序——Win32 键盘注入（这才是「不拘泥于网站」）

目标不是浏览器：可能是个 WPF 客户端、一个 PDF 阅读器、一个内网 OA。这张卡和上一张结构几乎一样，**只换了引擎和定位方式**。

```toml
schema  = 1
name    = "any-desktop-app"
channel = "desktop"

[target]
title   = "客户管理 - *"          # 窗口标题，支持通配；或
# exe   = "oa-client.exe"        # 按进程名匹配
# class = "Chrome_WidgetWin_1"   # 按窗口类名
focus   = true                   # 采集前 SetForegroundWindow（E2 必须）
restore = true                   # 结束后把焦点还给用户原来的窗口

[harvest]
mode       = "sendinput"         # sendinput = Win32 真实按键
pre_keys   = ["ctrl+home"]       # 先回到顶部
select_all = true                # Ctrl+A
copy       = "ctrl+c"            # Ctrl+C
timeout_ms = 3000                # 等剪贴板序列号变化，超时即报错

[paging]
style      = "key"
next       = "pagedown"          # 或 "ctrl+right"、"{scroll:3}"
max_pages  = 20
pause_ms   = 800

[segment]
mode        = "anchor"
anchor      = '^(?!\s).{1,24}$'
anchor_next = '^\d{4}\s*[-/.年]\s*\d{1,2}'

[fields.raw]
kind  = "line"
index = 0

[dedupe]
key = ["raw"]
```

**E2 的硬约束**：它会占用前台。所以卡里有 `restore = true`——采完把焦点还给用户原来的窗口。这条不影响功能但决定体验，不写进卡就写进默认值。

### 4.4 例三：需要精确字段时——DOM 选择器模式

只有「复制出来的文本里字段粘在一起」时才需要选择器。这是**可选精修**，不是必需前置。下面这个例子抓的是后台订单列表（电商后台、招聘后台、财务后台，结构一样）。

```toml
schema  = 1
name    = "admin-order-list"
channel = "browser"

[fetch]
url     = "https://admin.example.com/orders?page={page}"
ready   = "table.data"                           # 等到这个选择器出现再读 DOM
expand  = ["#filter-all", "button.refresh"]      # 先点开的元素（CSS）
scroll  = { until = "stable", max = 30, pause_ms = 700 }

[harvest]
mode = "dom"                                     # dom = 读 DOM，不发 Ctrl+C

[segment]
mode = "dom"
item = "table.data tbody tr"

[fields.order_no]
kind = "dom"
sel  = "td:nth-child(1)"

[fields.created]
kind = "dom"
sel  = "td:nth-child(2)"
type = "date"

[fields.amount]
kind = "dom"
sel  = "td:nth-child(3)"
type = "float"

[fields.status]
kind = "dom"
sel  = "td:nth-child(4)"

[fields.note]
kind = "dom"
sel  = "td:nth-child(5)"
type = "text"
```

### 4.5 例四：纯文本卡（只写切分规则，不含采集段）

这张卡没有 `channel` / `[harvest]`——它是**纯解析卡**，用来处理已有文本：`run --text-file a.txt -p card.toml`，或给 `add` 补解析历史数据。采集卡（4.2–4.4）里的 `[segment]`/`[fields]` 段与它完全同构，所以调试切分规则时可以拿文本文件离线迭代，不必反复开浏览器。

下面这张是**通用"条目块"卡**：每条记录由一个短标题行开头，紧跟一行日期。换成房源、招聘、政务名录、文献列表都一样用——改的只是字段名。

```toml
schema  = 1
name    = "generic-item-block"

[segment]
mode        = "anchor"
anchor      = '^(?!\s).{1,24}$'                              # 候选起点：短行、首字符非空白
anchor_next = '^\d{4}\s*[-/.年]\s*\d{1,2}'                    # 联合证据：下一行是日期
min_lines   = 2

[fields.title]
kind  = "line"
index = 0

[fields.date]
kind    = "search"
pattern = '(?P<v>\d{4}-\d{1,2}-\d{1,2})'
scope   = "head"
window  = 2                               # 只在前 2 行找，正文里的日期不误命中
type    = "date"

[fields.body]
kind = "span"
from = 'after:date'                       # 日期行之后
to   = 'before:^规格[:：]'                 # 到标记行之前；该行不存在时取到记录末尾
type = "text"

[fields.spec]
kind    = "search"
pattern = '^规格[:：]\s*(?P<v>.+)$'

[dedupe]
key          = ["title", "date", "body"]
on_collision = "report_only"

[paging]
style = "none"
```

**边界判定的唯一规则**：起点必须由**两条相邻行的联合证据**确认。单靠行内特征（长度、是否含数字、黑名单）区分「昵称」和「正文里的短行」在原理上不可分——实测里正是这个错误把 1 条记录切成 3 条。卡里 `anchor` 与 `anchor_next` 必须成对出现；只写一个时 `cards validate` 直接拒绝。

### 4.6 取数原语（`kind` 闭集）

| kind | 参数 | 语义 |
|---|---|---|
| `dom` | `sel`、`attr`（`text`/`html`/属性名）、`index` | 渲染后 DOM 里取 |
| `cell` | `col`（int）或 `name` | 表格列：HTML table / TSV / Markdown 表，列边界是事实 |
| `line` | `index`（负数从尾数） | 记录内第 N 行整行 |
| `search` | `pattern`（含 `?P<v>`）、`scope`（`record`/`head`/`tail`）、`window` | 记录内正则 |
| `span` | `from`、`to`（`after:field` / `before:^pat` / `line:N`） | 多行区间，供 `type=text` |
| `json` | `path`（`data.items[].title`） | 接口返回 |
| `const` | `value`（可含 `{var}`） | 来源标签列。**只放标注，不补数据** |

闭集必须小。凡是能靠这七个原语表达不了的版式，宁可进 `unparsed`，也不新增第八个原语。

---

## 5. `infer`：首跑零配置

目标：手上有一坨文本（`harvest` 刚抓到的、或文本文件、或你手动复制的一次），一条命令拿到表，**不需要知道「卡」这个概念存在**。

### 5.1 判定阶梯

| 级 | 判据 | 产出 |
|---|---|---|
| S0 | 有 CF_HTML 且含 `<table>`，或 TSV 且列数一致 | 直接 `cell`，列名取首行或 `c1..cn` |
| S1 | 行流里出现周期性锚点（同一正则连任 ≥3 次，间隔方差小） | `anchor` + `anchor_next` 草案 |
| S2 | 行内 `k[:：]v` 覆盖 ≥3 个不同键 | 单记录 + 键值对展开 |
| S3 | 行首有重复的符号/编号模式（`1.`、`-`、`[`） | 一条一项，单列 |
| S4 | 以上都不成立 | 一行一记录、单列 `raw` |

**S4 不是失败**：它把「结构不存在」这个事实交付出去，而不是编一个结构。报告首行写：「S4 兜底，未识别到记录结构，建议用 `--template` 指定，或让 agent 按 §8 换通道再抓一次」。

### 5.2 infer 的输出是三样东西

1. 一张表（当次直接可用）；
2. 一份卡草案 `cards/<name>.toml`（用户下次同站直接复用）；
3. 一行自评：`S2 ｜ 字段 4/5 ｜ 覆盖 97% ｜ 未识别 3 行`。

### 5.3 字段类型推断

按列取值分布定：全数字且长度 4 → `int`；命中 ≥80% 日期格式 → `date`；平均长度 > 40 且含换行 → `text`；其余 `str`。**类型只影响输出格式（xlsx 存数字还是文本），不影响抓取结果**——判错不致命，所以不做二次确认。

---

## 6. 累积采集：全自动翻页，去重入表

一次命令翻 N 页，中间没有人。所以写入口是 `harvest`，落同一个 dataset 结构：

> **命令分工**：`harvest` 是主命令（全自动一条命令跑完）；`add` 只做「把已有 raw 追加进 dataset」，供脚本串联；`run` 一次性出表不积累。

```
reviews/
├─ rows.csv            # 累积结果，列 = 卡的字段顺序
├─ state.json          # 已见记录 hash、翻页游标
├─ manifest.json       # 每次 harvest 的通道/引擎/新增行数/冲突数
└─ raw/0001.raw 0001.meta.json …
```

| 规则 | 做法 |
|---|---|
| 追加 | 按 `[dedupe].key` 判同一行；key 为空则不去重、全收 |
| 冲突 | `report_only`（默认，报告冲突数）/ `keep_first` / `keep_all`（加 `_dup_of` 列） |
| 断点 | 每页立即落 `raw/`，进程被杀不丢已采页；`state.json` 记录「翻到第几页」可续 |
| **翻页终止** | 三个条件任一命中即停：达到 `max_pages`、「下一页」元素消失、**连续 2 页新增行数为 0**（说明到底了或卡失效） |
| **节流** | 页间默认 `pause_ms=1200`，可按卡调；**不提供并发翻页**——并发会让「像人」这个前提失效 |
| 幂等 | 同一份 raw 重复 add 不产生新行（hash 已在 state 里） |

一条命令跑完（`E1` 浏览器 / `E2` 桌面，卡里声明，命令不变）：

```bash
# 浏览器：开页 → 展筛选 → 滚动 → Ctrl+A → Ctrl+C → 抽表 → 点下一页 → 循环
clip2table harvest -p cards/some-hard-site.toml -d orders/ --max-pages 20

# 任意 Windows 程序：聚焦窗口 → Ctrl+Home → Ctrl+A → Ctrl+C → PageDown → 循环
clip2table harvest -p cards/any-desktop-app.toml -d orders/ --max-pages 10

clip2table export -d orders/ -f csv,xlsx,md
```

**放量前的强制门**：`--max-pages > 1` 时，工具先跑第 1 页并打印前 3 条解析结果 + 报告首行，**等一次回车**才继续。这是唯一的交互点，理由是「50 页垃圾比 1 页垃圾贵 50 倍」。`--yes` 可跳过（供 agent 与脚本调用，agent 必须先看过一次单页报告才允许加 `--yes`）。

**为什么放量前要先看一页**：一个解析错误会以 `max_pages` 的倍数复制垃圾。所以第 1 页必须打印出来给人（或 agent）看一眼，才对得起后面 49 页。


---

## 7. CLI

| 命令 | 作用 | 需要什么 |
|---|---|---|
| `clip2table`（无参） | 等价 `run --auto`：读剪贴板 → 出表到当前目录 | 剪贴板 |
| `run` | 一次性：按卡或 auto 取一页 → 出表 → 打印报告 | 卡或 auto |
| `add` | 追加进 dataset：去重、续采 | dataset |
| `harvest` | **主角：一条命令跑完一整轮**——聚焦目标 → 展开 → 滚动 → Ctrl+A → Ctrl+C → 抽表 → 翻页，循环到 `max_pages` | 卡 |
| `grab` | 只取不解析（只做 ③④⑤），落 raw，供调试与 agent 中间态 | 卡 |
| `infer` | 从 1–3 份样本反推卡草案 | 样本 |
| `export` | 从 dataset 重出任意格式 | dataset |
| `cards` | `list` / `show` / `validate` | — |
| `test` | 用卡自带 samples 回归，列失败记录号 | 卡 |
| `doctor` | 自检：剪贴板格式、CDP 端口与浏览器路径、目标窗口可达性、Python 版本；给可直接粘贴的浏览器启动命令 | — |
| `selftest` | SKILL.md 里的命令与真实 argparser 对账 | — |

公共旗标只有六个：`-p/--card`、`-d/--dataset`、`-f/--format`（可重复）、`--channel`、`--json`、`--dry-run`。

**退出码三档**：`0` 成功（含「表出了但有 unparsed」，细节看报告）；`2` 输入或配置问题（剪贴板空、卡非法、通道不可用）；`3` 内部错误。调用方只需要这两条分支，不需要背十个数。

---

## 8. SKILL.md：给 agent 的决策循环

`SKILL.md` 里不许出现参数表——那是 `--help` 的活。它只回答「遇到什么信号，做什么动作」。

| 步 | 看什么信号 | 做什么 | 产物 |
|---|---|---|---|
| 1 判站 | 这站能不能直连？（`curl` 一次看返回） | 能直连 → A；401/403/空壳/要登录 → B；不是浏览器 → C | 一句「走哪条、为什么」 |
| 2 判引擎 | B 通道：有调试端口吗？C 通道：窗口找得到吗？ | `doctor` 查；没端口就给用户一行启动命令；窗口找不到就让用户先打开它 | 决定的引擎 E1/E2 |
| 3 取一页 | 先要一页，别要全部 | `harvest -p card.toml --max-pages 1 --dry-run` | 一份 raw + 格式清单 |
| 4 出初表 | 有卡 → `harvest`；无卡 → `infer` | 看报告首行：通道 + 引擎 + 分层 | 一张表 + 卡草案 |
| 5 自查 | 报告三项：unparsed 行数、各字段填充率、通道引擎 | unparsed 高 → 改 segment；单字段空 → 加 `scope/window`；级别是 S3/S4 → 换通道 | 修一次，再跑 |
| 6 放量 | 一页对了吗？对了才放量 | `harvest --max-pages N --yes` → `export` | 完整数据集 + 报告首行 |

**四条纪律写进 SKILL.md 正文**

1. **先一页，后放量**。没验证过一页就翻 50 页 = 50 页垃圾，且对目标站点造成 50 倍压力。
2. 表里没有的值不许补。空就是空。
3. 报告首行必须转述给用户：本次通道、引擎、本次页数、新增行数、unparsed 条数。
4. **两退一换**：同一页面重试两次仍拿不到结构 → 换通道（B→C 或 C→B），不要继续调正则；换通道仍失败 → 停下来把 `unparsed` 原文给用户看，请他把值在哪一行指出来，再改卡。

**agent 不该做的事（写进 SKILL.md 的「禁止」段）**

- 不主动提高按键频率、不并发多窗口去「抢时间」——节流是产品特性不是性能缺陷。
- 遇到验证码：停下，让用户在浏览器里过，然后 `harvest` 继续。**不尝试绕过**。
- 不为了「填满表格」而重跑失败页超过两次。
- **不在前台有用户正在打字的窗口时跑 E2**——先确认目标窗口，再抢焦点。

`selftest` 会解析 SKILL.md 里的每条 `clip2table <sub>`，对真实 argparser 校验存在性——两份工件不做对账，半年后必有一份说谎。

---

## 9. 目录树与技术选型

```
clip2table/
├─ pyproject.toml  README.md  SKILL.md  VERIFY.md  LICENSE
├─ src/clip2table/
│  ├─ cli.py  core.py  models.py  errors.py  report.py
│  ├─ ladder.py         # A→B→C 通道阶梯：每档都要说清自己为什么不行
│  ├─ harvest.py        # 一轮采集的状态机（开页→可见→校验→滚→复制→抽→翻页）
│  ├─ dataset.py        # rows.csv / state.json / manifest.json / raw/（断点续采、去重、冲突）
│  ├─ channels/
│  │  ├─ http.py        # 通道 A：urllib + 空壳/拦截识别 + gzip/charset 回退
│  │  ├─ cdp.py         # 通道 B：/json/list 目标发现、Runtime.evaluate、Input.dispatchKeyEvent
│  │  ├─ ws.py          # 最小 RFC6455 客户端（socket，仅文本帧）
│  │  ├─ keyboard.py    # 通道 C：窗口定位、SendInput、焦点保存/还原、focus_text_control
│  │  ├─ clipboard.py   # Win32 ctypes：TEXT / CF_HTML + 序列号监听
│  │  └─ cfhtml.py      # CF_HTML 字节偏移解析 + RTF 降级纯文本
│  ├─ pipeline/
│  │  ├─ sniff.py  normalize.py  segment.py  extract.py  assemble.py  htmltable.py  infer.py
│  ├─ cards/
│  │  ├─ schema.py  loader.py  regress.py
│  │  └─ builtin/*.toml + builtin/samples/<卡名>/{样本.txt,样本.expect.tsv}
│  ├─ render/  text.py（csv/md/jsonl）  xlsx.py（自写 zipfile + Sheet XML）
├─ tests/  fixtures/
```

`infer` 落在 `pipeline/` 而不是 `cards/`：它是从文本猜结构，不是从卡读结构。`selftest` 与 `test` 分别落在 `cli.py` 与 `cards/regress.py`——前者核对 SKILL.md 与真实子命令是否一致，后者用卡自带 samples 做回归，两者不是同一件事。

| 项 | 选择 | 理由 |
|---|---|---|
| Python | **≥3.11** | `tomllib` 进 stdlib。不为 3.10 造兼容层 |
| 配置格式 | **TOML** | 文件里全是正则：单引号零转义、无隐式类型、无缩进语义 |
| 剪贴板 | **ctypes / Win32** | 唯一能读 HTML 与 RTF 的零依赖路径。tkinter 读不到 HTML（实测抛 `form "STRING" not defined`），且每次建 Tk 要 95–109 ms，弃用 |
| 浏览器 | **CDP + 自写 ws**，连用户已开的 Edge/Chrome | 复用真实登录态与真实指纹。不引 playwright/selenium——它们会新开一个**干净的**浏览器，恰恰丢掉本项目唯一的优势 |
| 自动复制 | **E1 `Input.dispatchKeyEvent` 发真实 Ctrl+C + E2 Win32 `SendInput`** | E1 让浏览器自己写剪贴板（不依赖 `navigator.clipboard` 权限）；E2 让任何 Windows 程序分不清是脚本还是人（OS 级同一通道）。两者都免选择器 |
| HTML | **html.parser** | stdlib 够用；不引 lxml/bs4 |
| xlsx | **自写 zipfile + sheet XML** | 只写值与表头，不写样式，约 150 行换掉 openpyxl |
| 分发 | uvx / pipx | 零依赖才有秒级冷启动 |

**五条会让人踩坑的实现事实**（不进主线，但每条都是花了时间才测出来的）：

1. Win32 读剪贴板必须显式声明 `restype`（`GetClipboardData`→`HANDLE`、`GlobalLock`→`c_void_p`），否则 64 位指针被截断、直接 access violation。
2. CF_HTML 是 UTF-8 字节流且头里的偏移是**字节**偏移，按字符切会错位。
3. **`Input.dispatchKeyEvent` 必须成对发 `keyDown` + `keyUp`，并带 `modifiers=2`（Ctrl）和正确的 `windowsVirtualKeyCode=67`**；只发 `char` 事件浏览器不会触发复制。发完要等剪贴板序列号变化，不能立即读。
4. **`SetForegroundWindow` 返回非零不等于切过去了**（实测 7/8，失败那次前台是 0）。必须复读 `GetForegroundWindow` 校验；`AttachThreadInput` 才是真正能让它落地的那一步，且**必须在 finally 里 detach**，否则两个输入队列被粘住。
5. **WinUI3 程序（Win11 记事本、一切 WinAppSDK）程序化激活后焦点停在 `InputSiteWindowClass`，按键被吞而 `SendInput` 照样报成功**。判据是「可见的文本控件」：同一窗口下 6 个 `RichEditD2DPT` 里只有 `IsWindowVisible` 为真的那个能复制（`GWL_STYLE & WS_VISIBLE` 全为真，只有 `IsWindowVisible` 能分开）。

RTF 一律降级为纯文本（不做表格还原）。

---

## 10. 里程碑

### M1 — 剪贴板闭环（能读、能切、能出表）

范围：剪贴板读取层（Win32 ctypes，TEXT + CF_HTML）+ `sniff/normalize/segment/extract/assemble` + csv/md 导出 + `infer` 的 S0/S3/S4 + 无参命令。

| # | 验收 |
|---|---|
| 1 | 从 Excel 复制 3×10 区域，跑 `clip2table` 无参 → 10 行 3 列，逐字符相等 |
| 2 | 从网页表格复制（只有 CF_HTML、无纯文本）→ 走 S0，列数与页面一致 |
| 3 | 一段纯文本条目列表（三类不同版式各一份：电商列表、政务名录、招聘条目）→ `infer` 各自出表，报告写明 S1/S3 级别与填充率 |
| 4 | 空剪贴板 → 退出码 2、一句人话、无 traceback |
| 5 | `--dry-run` 前后目录文件数不变 |
| 6 | 读剪贴板前后，剪贴板内容与序列号不变（只读，绝不写回） |

### M2 — E1 自动按键（浏览器全自动，这是技术核心）

范围：CDP 通道 + `Input.dispatchKeyEvent` 自动 Ctrl+A/Ctrl+C + `[harvest]`/`[paging]` 卡段 + 自动翻页 + 卡 schema/loader/validate + xlsx。

| # | 验收 |
|---|---|
| 1 | **真实 Ctrl+C 能写剪贴板**：`harvest` 从已登录页面复制出非空文本，与手工 Ctrl+C 结果逐字符一致 |
| 2 | **选择器零依赖**：用一张只有 `[harvest] mode="copy"` 的卡（无任何 `dom` 字段）从某真实站点取到 ≥20 行 |
| 3 | **自动翻页**：`--max-pages 3` 点 3 次下一页，去重后行数 > 单页行数，且第 2/3 页内容与手工点进去看到的一致 |
| 4 | **前台不被打断**：窗口可见时，采集中用户切到别的窗口打字，采集不中断、前台不移动；浏览器最小化时必须借前台，采完归还并写进报告 |
| 5 | `doctor` 发现调试端口未开 → 输出一行可直接粘贴的浏览器启动命令（带 `--remote-debugging-port`） |
| 6 | `cards validate` 对 8 类错误卡逐个定位到键；`test` 输出失败记录号，故意改坏一条规则后失败数上升 |
| 7 | 页面出现验证码时：`harvest` 停下并提示用户手动处理，不重试、不报 traceback |

### M3 — E2 桌面注入（「不拘泥于网站」的兑现）

范围：`desktop` 通道 + 窗口定位 + `SendInput` 按键 + 焦点保存/恢复 + `[target]` 卡段。

| # | 验收 |
|---|---|
| 1 | **任意桌面程序可复制**：对一个非浏览器的 Windows 程序（客户端/PDF/记事本）跑完一轮，取到 ≥20 行且与手工 Ctrl+A/Ctrl+C 一致 |
| 2 | **焦点归还**：采集结束后前台窗口回到用户原先那个窗口 |
| 3 | **前台锁失败要报错**：目标窗口被其他程序抢占时明确报错退出，不静默产出空数据 |
| 4 | **同一张卡换引擎**：把 M2 的卡 `channel` 改成 `desktop` 并加 `[target]`，不加任何其他配置就能跑通 |
| 5 | 无卡时降级 A→B→C，三条路径各有一次真实走通记录，报告首行与之一致 |

### M4 — 放量 + 分页可靠性

范围：`dataset`/`state` + 断点续采 + `jsonl` + 翻页终止三条件。

| # | 验收 |
|---|---|
| 1 | 翻 5 页 `harvest` → 去重后行数 = 各页并集，重复行 0 |
| 2 | 第 3 页时 kill 进程，重跑续采：不重复、不丢页 |
| 3 | 连续 2 页新增 0 行 → 自动停，报告写「到底或卡失效」而不是继续空转 |
| 4 | `export` 一次出 csv/md/xlsx/jsonl；1 万行 xlsx 写出 < 3 s |
| 5 | 同 key 不同来源冲突按 `on_collision` 处理，报告列出冲突数 |
| 6 | 内置 5 份可用卡（含 ≥2 份 desktop 卡），每份都有 samples 且 `test` 通过 |
| 7 | `selftest` 通过；故意在 SKILL.md 写一个不存在的子命令 → 非 0 退出 |

---

## 11. 动手前先验的四个技术点（各 30 行原型）

顺序按「错了会改设计」的代价排。四点全过 → 直接进 M1；任何一点不过 → 换实现路径，不动上层设计。

| # | 要验什么 | 怎么验（当天可完成） | 不过怎么办 |
|---|---|---|---|
| 1 | **E1 能不能真把内容写进剪贴板** | 连本地 Edge 的 CDP，对一个人人可访问的长页面发 `keyDown/keyUp`（`modifiers=2`, vk=67），轮询 `GetClipboardSequenceNumber()` 看是否变化 | 退路：`Runtime.evaluate('document.getSelection().toString()')` 直接拿选区文本。丢 CF_HTML，但文本照样入表——流水线完全不用改 |
| 2 | **E2 的 `SendInput` 能不能让非浏览器程序响应 Ctrl+A/Ctrl+C** | 对记事本、再对一个自绘 UI 客户端（微信/钉钉）各发一次，比对剪贴板内容 | 改用 `PostMessage(WM_KEYDOWN)` 定向投递（不抢焦点，但部分程序不认）；两条都不过则该程序只走文本文件导入 |
| 3 | **CF_HTML 的字节偏移解析** | 复制一个中文网页表格，解析 `StartHTML/EndHTML` 后按字节切片，验证不出现半个汉字 | 只取纯文本 + TSV 两路；表格结构靠 `sniff` 从 TSV 的 `\t` 反推 |
| 4 | **E2 的前台锁在真实桌面上是否总能拿到** | 连续 20 次在「用户正在其他窗口打字」的状态下 `SetForegroundWindow` 目标窗口，统计成功率与恢复率 | 成功率 < 80% 就在采集期间用「最小化其他窗口」或要求用户空闲；不静默失败 |

**第 1 条是唯一的真赌注**：它决定「浏览器全自动」这条主线成不成立。退路也写在表里了——即使 E1 的复制失败，用 `document.getSelection()` 仍能全自动取到文本。**所以这个项目的全自动能力不依赖任何单点**：E1 复制通 → 有 HTML 结构；不通 → 有纯文本结构；E2 通 → 覆盖整个 Windows；只有读剪贴板这一环是刚性的，而它是最成熟的一环（Win32 API 三十年没变）。
