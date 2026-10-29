# 订单台账

把后台订单列表整理成一张能按订单号核对、能一直往下接的台账：订单号、客户、金额、状态、下单时间、更新时间六列。

目录里有三页输入、一张任务卡、四份独立答案、一个答案生成脚本和一个本地网页服务。三页不是「三页数据」，是**同一批订单被观察了三次**——第二页是新订单，第三页混进了晚到的旧版本、同时间的冲突和一条全新订单。这正是台账要能处理的事情。

| 文件 | 内容 |
| --- | --- |
| `input/page-1.txt` | 第 1 次观察：6 单 A20260314001–006 |
| `input/page-2.txt` | 第 2 次观察：5 单 A20260315001–005（含 `¥1,204.50` 千分位） |
| `input/page-3.txt` | 第 3 次观察：6 行，含晚到的旧版本、同时间不同内容、新订单 A20260316001 |
| `output/expected.csv`、`output/expected.md` | 前两页的台账答案（11 单，6 列） |
| `output/expected-current.csv` | 三次观察后的当前台账（12 单 + `_pending` 列） |
| `output/expected-changes.csv` | 相对两页基线的变化（含一条 `pending`） |
| `output/expected-pending.csv` | 需要人判断的（1 条） |
| `make-answers.py` | 生成后三份业务答案。**刻意不 import clip2table** |
| `order-ledger.toml` | 任务卡：6 个列字段、按订单号去重、按更新时间判新旧 |
| `serve.py` | 把三页输入呈现为 `/orders?page=1..3` |

`make-answers.py` 独立实现是刻意的：答案由被测代码生成的话，「输出与答案一致」就是同义反复。它只读三页输入，按卡里用中文写明的三条规则算结果；样本改了而答案没重新生成，`--check` 会红。

```powershell
python scenes/order-ledger/make-answers.py --check
```

## 本地三页采集

启动示例页面：

```powershell
python scenes/order-ledger/serve.py
```

另开一个终端（`NO_PROXY` 让本机请求直接打到示例服务）：

```powershell
$env:NO_PROXY = "127.0.0.1,localhost"
python _dev/c2t.py task new --text-file scenes/order-ledger/input/page-1.txt --channel http --source 'http://127.0.0.1:8765/orders?page={page}' --max-pages 3 --yes --out out/orders.toml
python _dev/c2t.py task check out/orders.toml --text-file scenes/order-ledger/input/page-1.txt --lines 10
python _dev/c2t.py task check out/orders.toml --text-file scenes/order-ledger/input/page-1.txt --confirm
python _dev/c2t.py task run -p out/orders.toml -d out/orders --yes
python _dev/c2t.py export -d out/orders -f csv -o out/orders.csv
```

`task run` 按卡里保存的地址连取三页，每页落地即提交。`export` 给的是「抓到过的全部行」，12 条。已安装版本把 `python _dev/c2t.py` 换成 `clip2table`。

只跑前两页对照 `expected.csv`：把 `--max-pages 3` 改成 2。

## 三次观察之后看台账

抓完三页，`out/orders` 里存的不只是 12 行，还有三份原文、三份解析结果和三个结果版本：

```powershell
python _dev/c2t.py rev list -d out/orders
```

```
★ v-…  3 份原文  完整  run: order-ledger
  v-…  2 份原文  完整  run: order-ledger   ← 基于 v-…
  v-…  1 份原文  完整  run: order-ledger   ← 基于 v-…
```

`★` 是当前台账。四个视图：

```powershell
python _dev/c2t.py ledger -d out/orders -f csv -o out/current.csv
python _dev/c2t.py ledger -d out/orders --view changes --baseline <两页版本> -f csv -o out/changes.csv
python _dev/c2t.py ledger -d out/orders --view pending
python _dev/c2t.py ledger -d out/orders --view history --show 5
```

导出的 current / changes / pending 与 `output/expected-current.csv`、`output/expected-changes.csv`、`output/expected-pending.csv` 对应。能看到三件事：

- **A20260314001 仍是「已发货」**。第三页里它的更新时间是 `2026-03-14 08:00`，比第 1 页的 `09:12` 还早——晚到的旧版本按 `[ledger] sort = "business_time"` 进历史，不覆盖当前值。
- **A20260314004 进「待处理」**。两次观察的更新时间同为 `2026-03-14 11:20`，状态分别是 `已发货` 和 `已完成`。当前台账保留已接受的「已发货」，待处理列表保存「已完成」候选；选定后保存决定。
- **12 单全在**。第三页只覆盖 6 单，其余 6 单没被这次观察到——没看到不等于没了，按原样保留。

`--baseline` 必须显式给。不给就没有「上次」，「跟上次比变了什么」会退化成「跟当前选中的比」。

## 改规则重算，不重新采集

想要金额里的数字，不要货币符号和千分位：

```powershell
python _dev/c2t.py task fix out/orders.toml --field 金额 --kind search --pattern '¥\s*(?P<v>[0-9,]+\.[0-9]{2})' --text-file scenes/order-ledger/input/page-2.txt
```

先看整批效果，确认后写回卡：

```powershell
python _dev/c2t.py task fix out/orders.toml --field 金额 --kind search --pattern '¥\s*(?P<v>[0-9,]+\.[0-9]{2})' --text-file scenes/order-ledger/input/page-2.txt --write
python _dev/c2t.py replay -d out/orders -p out/orders.toml --show 3
```

`replay` 用已存的三份原文重新解析，**不重新采集**，生成候选版本，并逐条指出哪个字段从什么变成什么、在原文第几行。它不自动切台账：

```powershell
python _dev/c2t.py rev select <候选版本> -d out/orders
```

切换后 `active.json` 指过去，旧版本和原始原文都还在。同一份原文可以有多份解析结果——这是「改规则」和「重新抓一遍」的区别。

## 重复采集与中断恢复

独立运行从来源第 1 页开始，保留本次捕获与历史观察。相同请求重试时使用相同标识：

```powershell
python _dev/c2t.py task run -p out/orders.toml -d out/orders --yes --request-id orders-morning --json
```

中断后用返回的运行标识继续，使用已保存的规则和进度：

```powershell
python _dev/c2t.py task run -d out/orders --resume run-xxxx --request-id orders-continue --json
```

跨次采集的独立回归样例位于 `tests/test_operation_recovery.py`：两次运行均采第 1 页，验证五个实体、八次历史观察，以及新增、更新、晚到、缺席和冲突；另外使用本地 HTTP 服务验证重复运行从第 1 页开始。

## 预览与纠错

```powershell
python _dev/c2t.py task check out/orders.toml --text-file scenes/order-ledger/input/page-2.txt --field 金额 --lines 10
```

预览显示每个字段的值、它来自原文第几行、填充率、以及与卡里声明形状的偏离。空行与制表符在原文列里分别显示成 `·`、`→`，所以「差一行」看得见。

## 接入已登录后台

先启用浏览器调试端口并登录后台，用 `clip2table doctor` 看连接状态。把后台第一页表格复制成 `page.txt`，填实际地址与下一页按钮的 CSS：

```powershell
clip2table task new --text-file page.txt --channel browser --source 'https://your-site.example/orders' --next 'a.next' --max-pages 2 --out orders.toml
clip2table task check orders.toml --text-file page.txt --lines 30
clip2table task check orders.toml --text-file page.txt --confirm
clip2table task run -p orders.toml -d out/backend --yes
clip2table ledger -d out/backend
```

向导收集字段名、复合主键、来源和翻页配置。浏览器用已有登录会话并按 URL 定位标签页；按钮翻页从当前页开始，独立重跑前把页面切回第一页。后台支持页码 URL 时，用含 `{page}` 的 `--source`，任务会自己逐页导航。

## 换成你自己的后台

1. 登录后台，打开第一页表格，全选复制。
2. `clip2table task new --text-file 你的样本.txt --yes --out 你的任务.toml`，看它猜的记录与字段对不对。
3. 不对就 `task check` 看原文归属，用 `--record-start`/`--record-end` 标一条完整记录的起止，或 `task fix` 改一个字段。
4. 卡里补三件样本说不出的事：`[task].source`（下次去哪儿取）、`[dedupe].mutable`（哪些字段本来就会变）、`[ledger]`（按哪个时间判断新旧）。
5. `task run -d 你的数据集` 连续跑几次，每次都是新观察，`ledger` 会累积成台账。
