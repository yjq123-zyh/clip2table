# 使用说明

本文介绍安装、采集、结构识别、卡片与任务配置、导出方法。
模块职责见 `architecture.md`，设计取舍见 `design-notes.md`。

---

## 1. 它做什么

clip2table 是一个面向 Windows 的文本转表格工具。它从网页、桌面应用、剪贴板或本地文件
读取可复制的文本，识别记录和字段，导出 CSV、XLSX、JSONL 和 Markdown。

常见用途包括整理后台订单、公告列表、招聘信息、商品评价和本地台账。
浏览器和桌面通道通过复制取得内容，HTTP 通道直接请求网页，随后进入同一套解析流程。

```
网页 / 桌面应用 / 剪贴板 / 本地文件
                  ↓
读取内容 → 识别结构 → 划分记录 → 提取字段 → 去重与保存
                  ↓
       CSV / XLSX / JSONL / Markdown
```

---

## 2. 装

```bash
pip install clip2table
```

要在仓库里改代码时用 `pip install -e .`。

零必需依赖，只要 Python 3.11+（`tomllib` 在标准库里）。Windows 10/11 是主要目标平台，
因为桌面通道要用 Win32 剪贴板与 `SendInput`。

不想装包：`python _dev/c2t.py <子命令>` 会直接把 `src/` 挂上路径。

自检：

```bash
clip2table doctor
```

它会检查剪贴板、Python 版本、卡目录，以及浏览器调试端口。没有可用调试端口时，
它会打印一行**可以直接粘贴**的浏览器启动命令（路径是探测出来的绝对路径，profile 目录带端口号——
共用 profile 会让调试端口被静默忽略）。

---

## 3. 三条通道

同一个 `Captured` 对象进同一条流水线，区别只在"怎么拿到文本"。

| 通道 | 引擎 | 用在哪 | 代价 |
|---|---|---|---|
| `http` | HTTP 直连 | 服务端就渲染好了 | 无 |
| `browser` | E1：CDP 发真 Ctrl+A / Ctrl+C | 要登录、要 JS、有反爬 | 窗口只是被盖住时不动你的前台；浏览器被最小化时要短暂借前台，采完归还 |
| `desktop` | E2：Win32 `SendInput` | Windows 客户端、Excel、记事本 | 需要前台焦点（见 §11 安全边界） |

不指定时自动降级：先试直连（2 秒超时），被挡就驱动已登录的浏览器，还不行就驱动前台窗口。
`--channel` 可以钉死某一条，钉死时不降级。

**报告第一行永远写清楚这次走了哪条、为什么**，第二行是来源，例如：

```
通道 browser ｜ 引擎 E1-CDP ｜ 判定依据：直连返回 403，改用已登录浏览器
来源：https://example.com/list?page=1
```

`来源` 行的三种形态，都在说"我到底读到了什么"：

| 形态 | 含义 |
|---|---|
| `来源：<地址>` | 读到的就是这个地址 |
| `来源：<实际地址>（发出的请求是 <请求地址>，落地地址与请求不同）` | 回退换通道时落到了别处 |
| `来源：未知地址 ｜ 实际驱动的是窗口 '<标题>'` | 桌面通道读不到地址栏 |

桌面通道记录实际操作的窗口信息；地址无法取得时，来源地址显示为“未知”。

---

## 4. 五分钟上手

### 第一步：识别结构

```bash
clip2table infer                     # 读剪贴板
clip2table infer --text-file page.txt # 读文本文件（先把内容复制到文件里）
```

`infer` 只吃**已经拿到手的文本**：剪贴板或文件。要从地址取，先 `run --url ... --dry-run` 把内容取下来，
或者直接 `harvest` 一页再看报告。

`infer` 反推它看到的形状并给出一份可直接用的卡草案：

| 级别 | 含义 |
|---|---|
| S0 | 有真表格结构（Tab 分隔的等宽行） |
| S1 | 周期记录（每 N 行一条） |
| S2 | 键值对（`名称：值`） |
| S3 | 编号列表（一行一条） |
| S4 | 无结构（全进未识别） |

存在多种切分方式时，报告会列出候选结构及其依据，便于选择记录边界。

### 第二步：结构对了就固化成卡

```bash
clip2table infer --write-card cards/mysite.toml
clip2table cards validate cards/mysite.toml
clip2table harvest -p cards/mysite.toml --max-pages 1 --dry-run --show 3
```

`--dry-run` 预览结果，`--show 3` 打印前三条记录。确认记录边界和字段后，可增加采集页数。

### 更快：交给任务向导

手写卡不算难，但第一次总要试几遍。`task new` 把这一步变成问答：它先打印推断出来的表，
再逐条问字段名、去重主键、数据来源，最后写出一个**以后能直接再跑**的任务卡。

```bash
clip2table task new --text-file page.txt          # 交互
clip2table task new --text-file page.txt --yes    # 全部用推断值（agent 用这条）
```

向导收集采集通道、目标地址或窗口、翻页方式和页数。自动填写时可以显式传入：

```powershell
clip2table task new --text-file page.txt --channel browser --source 'https://your-site.example/orders' --next 'a.next' --max-pages 2 --yes --out cards/mysite.toml
clip2table task new --text-file page.txt --channel http --source 'https://your-site.example/orders?page={page}' --max-pages 2 --yes --out cards/http.toml
clip2table task new --text-file page.txt --channel desktop --source '订单管理' --next pagedown --max-pages 2 --yes --out cards/desktop.toml
```

`--source` 保存再次采集的目标；`--url` 用于立即读取样本。浏览器按钮翻页从当前页面开始，
重跑前切回第一页；页码 URL 会自动定位并逐页导航。`--yes` 配合文件样本且未提供来源时，
生成用于文件导入的规则卡，运行时继续提供 `--text-file` 或 `--html-file`。
复合去重键用逗号分隔，输入 `-` 关闭去重。转换已有卡（`task new -p <卡>`）是给这张卡补任务
元数据，不是重写它：冲突策略、身份与可变字段、标题/用途/来源/创建日期，以及 `[ledger]` 段都
原样带过去。

### 第三步：预览，确认每个字段来自原文哪一行

```bash
clip2table task check cards/mysite.toml --text-file page.txt --lines 20
```

三样东西一起打印：表格、每个字段的值来自原文第几行、原文每一行归哪条记录（`·` 表示
没人认领）。字段取错行时，看这里最快。

### 第四步：改一条规则，看整批效果，再保存

```bash
clip2table task fix cards/mysite.toml --field 金额 \
  --kind search --pattern '¥\s*(?P<v>[0-9][0-9,]*\.[0-9]{2})' \
  --text-file page.txt
```

不带 `--write` 只打印「这条规则会让哪些值变成什么」，卡不动。确认对了再：

```bash
clip2table task fix ... --write
```

`--write` 只替换那一个 `[fields.X]` 段，文件里其他字节（注释、排版）不动。

### 第五步：放量

```bash
clip2table task run -p cards/mysite.toml -d out/mytask --max-pages 20 --yes
clip2table export -d out/mytask -f csv -f xlsx --dir out
```

多页采集会要求交互确认，`--yes` 可直接开始执行。`-d` 让结果累积进一个目录，同一页
重复提交会被挡住并给出非零退出码。

`-d` 的导出要单独跑 `export -d`：采集与导出分开，是为了采集中断时已经提交的页仍然能
导出，而不是卡在最后一页才落盘。

---

## 5. 卡：加一个网站 = 加一个文件

卡是 TOML，不进代码。换站点只加文件，`clip2table` 一行不用改。

```toml
schema  = 1
name    = "notice-list"
channel = "browser"

[harvest]
select_all = true

[segment]
mode        = "anchor"
anchor      = '^(?!\s).{1,60}$'              # 记录起点：不缩进的短行
anchor_next = '^\d{4}-\d{1,2}-\d{1,2}\s*$'   # 第二行必须是裸日期
min_lines   = 3
record_lines = [3, 8]                        # 一条记录的行数范围（可选）
required    = ["title", "date"]              # 必须有值的字段（可选）

[fields.title]
kind  = "line"
index = 0

[fields.date]
kind    = "line"
index   = 1
type    = "date"

[dedupe]
key = ["title"]

[paging]
style     = "click"
next      = "a.next"
max_pages = 20
```

### `anchor` 和 `anchor_next` 必须成对

这是整个项目最重要的一条经验，而且是实测出来的：**单看一行判断"这是不是新记录"是错的。**

"昵称"和"正文的短行"在长度上没有区别。用长度阈值切，一条记录会被切成两条，
而每一条看起来都像模像样。改成"这一行像标题"**并且**"下一行是裸日期"两行联合，才能把它分开——
因为正文里提到日期的句子几乎不会**只有一个日期**。

（已知歧义：三个等距日期出现在一片平面文本里时，局部证据上和"三行一条的记录"无法区分。
这种情况工具会切，因为拒掉它会误杀真实的短记录。这类取舍写在代码注释里，不装作没有。）

### 卡可以声明记录的形状，偏离会被指出

`record_lines` 与 `required` 是可选的第二道校验：`anchor` 只决定"记录从哪开始"，
管不了"切出来的记录是不是卡写的那种形状"。站点保留锚点却少一行时，结果会是一张
每列都有值、每个值都错位的表。

命中偏离时：**报告里指出第几条记录的哪个期望没满足，记录照样保留**——它是警告，不是过滤器。

### 五条取数原语，另有两条明确不可用

**现在能用的五条**：

| 原语 | 取什么 |
|---|---|
| `line` | 记录的第 n 行（负数从末尾数） |
| `cell` | 表格的一列（HTML 表格或 TSV），列边界是事实 |
| `search` | 匹配正则的那一行，再取捕获组 |
| `span` | 两个标记之间的文本，可跨行。标记写法见下 |
| `const` | 固定值，**只用于标注来源，从不拿来填数据** |

`span` 的边界写成 `after:起点` / `before:终点`，参数可以是另一个字段名、
`line:3`、或一段字面文本。**标记找不到时默认报空并说明原因**，不会退化成"整条记录"
——那会把每行都当成这个字段的值，填得满满当当却是错的。

页面形状会变时（比如有的条目有「规格」行、有的没有），把标记写成 `before?:规格`
表示这个终点可有可无：在就切在它前面，不在就取到记录结尾。

**词表里还有 `dom` 与 `json`，但它们没有实现**：卡里写了会在 `cards validate` 阶段被拒绝，
并告诉你替代写法（`dom` 改用 `line`/`search`/`span`/`cell`；`json` 先存成文件走文本路径）。

之所以留着这两个词而不是删掉：旧卡写了它们会**响亮地失败**，而不是静默产出空值。
一个"页面没有这个元素"和一个"这个版本根本做不到"如果都显示成空格，用户就永远不知道是哪一种。

表达不了就进未识别。**宁可少给一列，也不猜一个值。**

---

## 6. 内置卡

| 卡 | 通道 | 形状 |
|---|---|---|
| `notice-list` | browser | 标题 / 日期 / 正文，可带规格行 |
| `admin-order-list` | browser | 后台表格，列边界即字段边界 |
| `generic-item-block` | desktop | 标题 / 日期 / 多行正文 / 规格 |
| `job-list` | desktop | 职位 / 公司 / 地点 / 薪资 / 日期（日期在末尾） |
| `numbered-list` | desktop | 一行一条的编号台账 |

每张卡都自带样本和答案键，`clip2table test <卡名>` 会跑回归。

---

## 7. `validate` 和 `test` 不是一回事

```bash
clip2table cards validate mycard.toml   # 卡合不合法：语法、正则、字段引用、声明的能力是否真的存在
clip2table test mycard.toml             # 卡的规则对不对：拿样本逐字段比答案
```

`validate` 通过但 `test` 失败，是最常见也最危险的状态——**规则合法但是错的**。

答案键故意放在卡外面的独立文件里：如果期望值由工具自己生成，改坏规则时期望值同步变坏，
永远都能通过，那这个测试就等于没有。

---

## 8. 累积采集与断点续采

一次性导出用 `-o`；要跨批次累积用 `-d`：

```bash
clip2table harvest -p card.toml -d data/mysite --max-pages 5
# 明天采一轮新的观察
clip2table harvest -p card.toml -d data/mysite --max-pages 5
# 任意时候重出
clip2table export -d data/mysite -f csv -f xlsx --dir out
```

新建数据集使用结果版本布局，独立采集与恢复的用法见第 10 节。
本节以下 pages/state 提交与续采规则适用于已有旧布局数据集。

### 以「页」为提交单位

每采一页就落一个不可变的页目录（`pages/0001/`），里面存原文、来源信息、解析结果、
卡片版本、下一步位置。重跑从已提交的页往后接，**半页不会被当成整页**。

一次提交里同时固定了四件事：来源范围、卡片快照、字段定义、主键规则。
改了卡再往同一个 dataset 追加会被拒绝——同名列在不同规则下含义不同，混在一起
看起来齐整、实际上不是一种数据。确实要混，用 `--allow-card-change` 显式声明。

### 冲突分成两种，都落盘

同一主键在后续页里换了内容，和"两个不同的东西撞了同一个键"，是两件事：

- **内容变更**：卡声明的身份字段没变，只有其它字段变了。默认保留**先到**的那条，
  新值留在 `conflicts.json`（`on_collision = "keep_all"` 时两条都进表）。
- **身份冲突**：卡声明为身份字段的列变了，可能根本不是同一条记录。处置同上，
  绝不会静默合并。

判定依据来自卡，不来自"改了几个字段"：

```toml
[dedupe]
key = ["订单号"]
identity = ["订单号", "下单人"]
mutable  = ["状态", "金额"]
```

不写 `identity` 时，工具只给提示（"主键可能选错"），不替你断言这是两个东西。

两种都写进 dataset 里的 `conflicts.json`，不是只打印在当次报告里。
**不要用"再跑一遍然后手工去重"代替它**——手工去重会把"两个不同的人发了同样的内容"
当成重复删掉，那正是这个项目要避免的丢数据。

### 翻页什么时候停

四个信号，先看明确信号，最后才看兜底：

1. 达到 `--max-pages`（你自己设的上限）
2. 「下一页」不可用（消失、点不动、按了翻页键没滚出新内容）
3. 页面内容重复（又回到了访问过的页）——`[paging].stop_when` 的默认值就是它
4. "连续 N 页没有新增"——只在 `[paging].stop_when = "empty"` 时启用，
   是可配置的兜底，不是主要判据

卡里**没有**「末页标记」这种配置。判断到底靠的是"下一页还在不在"和"内容是不是重复了"，
不靠站点自己告诉你——站点不说的东西，工具也不假装知道。

---

## 9. 任务：把一次成功变成可以重复跑的东西

卡描述「怎么切」，任务描述「这是一件什么事」。任务就是一张卡加上一段 `[task]`：

```toml
[task]
title    = '订单台账'
purpose  = '把后台订单列表整理成一张能按订单号核对、能一直往下接的台账'
source   = '订单管理后台 → 订单列表（登录后按页显示，每页 20 单）'
created  = '2026-10-25'
expected = '每单一行，订单号唯一；金额保持页面原文（含 ¥ 与千分位）'
```

这段**只给人看，不参与解析**：删掉它，新采的数据一个字都不会变。写它是因为一周后
回来，`admin-order-list.toml` 这个名字不会告诉你「这抓的是哪个后台、想要什么结果」。

任务命令：

```bash
clip2table task new --text-file page.txt        # 向导：生成卡
clip2table task show order-ledger.toml          # 看任务：字段、主键、跑过几次
clip2table task check order-ledger.toml --text-file page.txt   # 预览：字段对回原文哪一行
clip2table task fix order-ledger.toml --field 金额 --kind search --pattern '...'  # 改一条规则
clip2table task run -p order-ledger.toml -d out/orders          # 再跑一次
```

`task new` 是交互式的，会逐条问；加 `--yes` 全部取推断值——**给 agent 用的**。
一个只有人能敲字时才工作的向导，在自动化场景里等于不能用。

`task fix` 默认**不写盘**，只打印「这条规则会让哪些值变成什么」；确认后加 `--write`，
它只替换那一个 `[fields.X]` 段，文件里其他字节不动。整份重写会丢注释和排版，而
这种文件本来就是要手改的。

### 按原文标记记录和字段

先显示原文行号和记录归属：

```powershell
clip2table task check orders.toml --text-file page.txt --lines 30
```

如果输出列出编号候选，用 `--pick N` 预览另一种分段。此操作保留现有字段规则，
加 `--write` 保存所选分段。加 `--json` 返回完整 JSON，包括所选候选、
记录和字段来源行号。

```powershell
clip2table task check orders.toml --text-file page.txt --pick 1 --json
clip2table task check orders.toml --text-file page.txt --pick 1 --write
```

候选保存为 `mode = "periodic"`，包含 `period`（每条记录的非空行数）、`offset`（跳过的非空行数）
和 `count`（该区域的记录数上限）。区域前后的内容保留为未识别行；保存后重新加载使用同一规则。
规则用于固定布局区域，页面记录数超过 `count` 时，剩余内容会显示在未识别行中。

### 确认与任务指标

```powershell
clip2table task check orders.toml --text-file page.txt --confirm
clip2table task show orders.toml --json
```

检查结果分别显示字段填充率、规则偏离和人工确认状态。`--confirm` 记录用户对当前规则与样本的确认；
规则或样本变化后需重新确认。空结果、行数不守恒或必填字段等规则偏离会阻止确认。
确认候选时同时加 `--write` 保存。首次配置耗时从进入创建向导计至首次确认，后续确认保留原始耗时。
`task run --json` 返回单个 JSON 对象，`task_stats` 包含运行记录。

### 保存记录边界

保存记录边界时，标出首条完整记录在原文中的起止行。下面把第 3 到第 6 行作为示例：

```powershell
clip2table task new -p orders.toml --text-file page.txt --record-start 3 --record-end 6 --out corrected.toml --yes
```

两个边界参数一起使用，行号从 1 开始，结束行包含在示例中。行号包含空行；
记录长度按非空行计算。工具从起始行生成锚点，核对整份样本中的重复起点，
并用保存后的同一套分段规则预览。保存的 `record_lines` 会检查后续记录的长度变化。

字段取错行时，指定该字段应读取的原文行号。这一行可以属于任意一条已解析记录：

```powershell
clip2table task fix corrected.toml --field 金额 --from-line 8 --text-file page.txt
clip2table task fix corrected.toml --field 金额 --from-line 8 --text-file page.txt --write
```

也可以用第一条记录中只出现于一行的文字定位：

```powershell
clip2table task fix corrected.toml --field 金额 --value '¥100.00' --text-file page.txt --write
```

`--from-line` 和 `--value` 会生成读取整行的规则，将同一记录内位置应用到整批样本，
并保留字段类型和数值换算参数。只提取行中的一部分内容时，使用 `--kind search --pattern`。
空行、未归属记录的行、越界行号和多行命中的值会报出具体原因；修正加 `--write` 保存后，
再次执行任务会使用新规则。

`task run -d` 会把这次运行记进任务旁边的一个小文件（`任务名.stats.json`），
于是 `task show` 能说出：

```
任务记录：这是第 2 次运行，这次 12 秒，从建任务到确认结果正确用了 2 分 10 秒，确认过 1 次结果正确，改过 1 次规则
```

记录包含运行次数、每次运行耗时、首次配置至确认耗时、人工确认次数、修正次数及成功重跑次数。

记账文件坏了不会让采集失败：读不出来就当没有记录，继续采。记录不值这个代价。

---

## 10. 结果版本、持续台账与恢复

### 新采集、重试和恢复

| 操作 | 命令或入口 | 数据变化 |
|---|---|---|
| 新一次采集 | `task run -d 目录` | 创建新 run，从来源第一页开始，追加观察 |
| 重试同一请求 | 原命令加相同 `--request-id` | 输入相同时返回原结果 |
| 继续未完成操作 | `task run -d 目录 --resume <operation_id 或 run_id>` | 继续原操作和原 run，复用冻结配置与已保存内容 |
| 改规则重算 | `replay -d 目录 -p edited.toml` | 同一捕获生成新解析，形成候选版本 |
| 导出 | `ledger -d 目录 -f xlsx -o …` | 固定结果版本、策略和人工决定，生成文件 |

页号在每个 run 内从 1 开始。两次独立运行的第 1 页各自保留；内容相同也保留两次观察的时间与来源。台账按业务主键归并实体，history 保留观察明细。

指定请求标识后，可以重复提交同一条命令：

```powershell
clip2table task run -p orders.toml -d out/orders --yes --request-id orders-20261028 --json
```

同一标识绑定实际规则、来源配置和文件内容；输入改变时返回 request_conflict。
再次采集使用新的请求标识；省略时工具自动生成，结果中返回 request_id。

中断或失败后使用结果中的 operation_id 或 run_id 恢复：

```powershell
clip2table task run -d out/orders --resume run-xxxx --request-id orders-continue --json
```

恢复使用原运行保存的卡片和采集配置，原卡片文件移动、删除或修改都不改变这次运行。任务统计按 run_id 更新，恢复和同请求重试不会增加采集次数。

结果中的 pages/rows 是原运行已提交总量，captured_pages 是已保存原文数，this_request_pages/rows 是本次调用新增提交，dataset_rows 是当前台账规模。

遇到登录、验证或位置确认时，先完成来源页面上的操作，再创建回答文件。checkpoint 和 action_id 从最近的结果复制：

```json
{
  "checkpoint": {"operation_id": "op-xxxx", "generation": 1},
  "action_response": {"action_id": "action-xxxx", "confirmed": true}
}
```

```powershell
clip2table task run -d out/orders --resume op-xxxx --answer answer.json --request-id orders-after-verification --json
```

回答文件使用 UTF-8。目标选择动作需按候选列表提供 target_id；桌面和纯点击翻页续采需把来源定位到检查点的下一页。工具会核对动作归属和检查点代次，过期回答返回明确错误。

### 重解析与版本选择

```powershell
clip2table replay -d out/orders
clip2table replay -d out/orders -p edited.toml --show 10
clip2table rev list -d out/orders
clip2table rev show v-xxxx -d out/orders --json
clip2table rev select v-xxxx -d out/orders --expect v-baseline
```

重解析保留完整捕获集合，只替换指定捕获的解析引用。候选中保存字段变化、原文坐标、边界变化和逐捕获失败清单。失败页沿用旧解析供查看，operation_complete 为 false，修复后可通过 API resume 继续失败部分。

版本分别记录引用完整性、范围覆盖、操作完成和解析质量。行数守恒只是一项质量检查。
本次目标范围未处理完成的候选不能启用；`--allow-incomplete` 只允许显式接受质量不完整，不能把处理失败标成完成。

重解析默认生成候选；差异确认后可选择，也可在 replay 中加 `--select`。
无业务主键时，比较使用同一捕获中完整的原文行坐标；重复键和拆分/合并歧义分别列出。

### 四种台账视图

```powershell
clip2table ledger -d out/orders --view current
clip2table ledger -d out/orders --view changes --baseline v-baseline
clip2table ledger -d out/orders --view history
clip2table ledger -d out/orders --view pending
clip2table ledger -d out/orders --view current -f xlsx -o out/current.xlsx
```

current 每个已确定实体一行；history 每次观察一行；changes 给出相对明确基线的新增、更新和待处理项；pending 保存冲突候选。变化统计按实体计数，导出明细按字段展开。未变化单独计数。

卡片声明排序和允许变化的字段：

```toml
[dedupe]
key = ['订单号']
identity = ['客户']
mutable = ['状态', '更新时间']

[ledger]
sort = "business_time"
field = '更新时间'
timezone = "Asia/Shanghai"
```

排序支持业务时间 business_time、业务版本 business_version 和显式的观察时间 observed。
旧业务版本晚到留在历史中；未再次出现的订单继续保留已知当前值。

| 情况 | 处理 |
|---|---|
| 排序值相同但内容不同 | 保留已经接受的当前值，候选进入 pending |
| 身份字段变化 | 保留原身份，展示冲突候选 |
| 未声明可变的字段变化 | 进入 pending，附字段差异 |
| 排序值缺失或无效 | 保留候选；已有确定当前值继续可查 |
| 主键缺失或类型不符 | 单独保留待处理来源记录 |

主键 text 保留前导零；声明 int/float 时按精确数值归并。复合键保存各部分边界。
业务时间按声明时区比较，无法解释的时间值进入待处理。

人工选择候选后，决定追加保存，新的视图引用固定决定集合。相同冲突重复出现会追加来源；新候选或规则变化使原决定不再适用时，重新进入待处理。

### 旧数据的查看和采用

旧 pages/state 数据集继续使用原读取路径。查看原文和类型是否完整：

```powershell
clip2table rev legacy -d old/orders
clip2table rev adopt -d out/adopted --source old/orders 1 --kind text
```

采用后返回候选版本，再使用 rev select 显式选择。--source 和目标 -d 使用不同目录，源目录保留原样。后续采用更多页面时先选择已有候选，再追加下一页。

没有原文的旧页也可保留其已有行；捕获类型、原观察时间及解析器等缺失信息标记为未知。缺少原文的页面重算会列出明确原因。旧 rule_id 继续可读，新规则身份仅由影响解析的配置决定，台账策略另存。

### Python 接口

```python
from clip2table import api
from clip2table.cards.loader import resolve_card

result = api.execute(
    api.ExecuteRequest(
        request_id="orders-20261028",
        dataset="out/orders",
        text_file="page-1.txt",
    ),
    resolve_card("orders.toml"),
)
print(result.status, result.counts, result.operation_id)
```

传入 Card 对象或请求中的 card 路径均可；实际规则内容参与请求摘要。
preview 返回样本及候选结构，不创建业务运行。普通输入和通道错误返回 failed 与稳定 error.code；
status 使用 completed / needs_action / failed / cancelled，reason 和 action 描述下一步。

来源操作完成后继续原操作：

```python
continued = api.resume(api.ResumeRequest(
    request_id="orders-continue",
    dataset="out/orders",
    operation_id=result.operation_id,
    checkpoint=result.checkpoint,
    action_response={"action_id": result.action["action_id"], "confirmed": True},
))
```

重启后用 operation_status 读取最新检查点与动作，再提交回答：

```python
status = api.operation_status(api.ResumeRequest(
    dataset="out/orders", operation_id="op-xxxx"
))
```

人工处理 pending 时，先读取候选并创建动作：

```python
rows, fields, view = api.ledger_view("out/orders", view="pending")
item = view.pending[0]
action = api.resolve(
    api.Request(dataset="out/orders", request_id="review-order"),
    pending_id=item.pending_id,
)
print(action.action["candidates"])
```

选定候选后，通过 resume 提交 action_response，其中 candidate 为该候选的 candidate_id，或 keep_pending。checkpoint 和 action_id 使用本次 action 返回值。

execute / resume / reparse / select_revision / export 共用请求登记、写锁与持久化回执。
请求已完成时重试返回原结果；已返回失败清单后继续处理，使用新的 request_id 关联原 operation_id 和 checkpoint。
导出固定版本、策略及决定集合，返回文件摘要、格式损失说明和完整值伴随文件位置。

---

## 11. 安全边界（desktop 通道）

E2 是用操作系统的按键通道往窗口里发键，**它能造成真实后果**。所以它有三道闸：

1. **发键前校验窗口身份**：句柄有效、标题匹配卡里的规则、进程名匹配。
   对不上就报错退出，一个键都不发。
2. **只发只读按键**：白名单只有九个——`Ctrl+A`、`Ctrl+C`、`Ctrl+Home`、
   `PageDown`、`PageUp`、`Home`、`End`、`Esc`、`Tab`。别的键名会被拒绝并列出可用项。
   代码里**没有**往窗口里打字的路径，这不是没实现，是禁止实现。
3. **焦点归还**：发键前后各记一次前台窗口，结束后还回去。失败会在报告里写明。

目标文档有未保存修改时默认拒绝，需要 `--force` 显式同意才继续。

（这些约束的每一条都来自一次真实事故：探针在发任何键之前没校验目标身份，
把测试字符敲进了正在编辑的文档缓冲区。文件最终没坏，但那条路必须封死。）

---

## 12. 使用条件

采集对象需要提供可选中、可复制的文本，适用于用户已登录的页面、桌面客户端列表、
Excel 选中区域和本地文本窗口。

折叠内容应先展开，验证码由用户完成。图片和 Canvas 内容需先通过 OCR 等方式转为文本。
采集范围应符合用户对目标系统和数据的访问权限。

---

## 13. 数据处理

源文本按归属记录到解析结果或未识别清单，报告统计两者占用的源行数。
未识别内容随导出结果保存为同名 `.unparsed.txt` 文件。

卡片中的 `record_lines` 和 `required` 用于逐页检查记录长度及必填字段。
`clip2table test` 将样本结果与独立答案文件逐字段比较。

原文缺少的字段保留为空。字段填充率低于 80% 时，报告会列出该字段的类型与提取失败原因。

---

## 14. 开发

```bash
python -m unittest discover -s tests -t .     # 全量
python _dev/c2t.py selftest                   # 文档里的命令与真实参数表对账
```

`SKILL.md` 描述 Agent 的操作流程，命令参数可通过 `--help` 查看。
`selftest` 在源码仓库中核对文档引用的子命令，发现不匹配项或缺少文档时返回非零退出码。
