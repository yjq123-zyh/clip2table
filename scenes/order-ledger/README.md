# 订单台账

将订单列表整理为订单号、客户、金额、状态、下单时间五列。目录提供两页输入、任务卡、独立答案和本地网页服务。

| 文件 | 内容 |
| --- | --- |
| `input/page-1.txt`、`input/page-2.txt` | 制表符分隔的订单样本，分别为 6 单、5 单 |
| `output/expected.csv`、`output/expected.md` | 手工整理的 11 单独立答案 |
| `order-ledger.toml` | 样本解析卡，保留金额原文，以订单号去重 |
| `serve.py` | 将样本呈现为两个 HTTP 页面 |

## 本地两页采集

在项目根目录打开终端，启动示例页面：

```powershell
python scenes/order-ledger/serve.py
```

在另一个终端执行以下命令。已安装版本可将 `python _dev/c2t.py` 换为 `clip2table`。
`NO_PROXY` 让本机请求直接访问示例服务。

```powershell
$env:NO_PROXY = "127.0.0.1,localhost"
python _dev/c2t.py task new --text-file scenes/order-ledger/input/page-1.txt --channel http --source 'http://127.0.0.1:8765/orders?page={page}' --max-pages 2 --yes --out out/orders.toml
python _dev/c2t.py task check out/orders.toml --text-file scenes/order-ledger/input/page-1.txt --lines 10
python _dev/c2t.py task check out/orders.toml --text-file scenes/order-ledger/input/page-1.txt --confirm
python _dev/c2t.py task run -p out/orders.toml -d out/orders-first --yes
python _dev/c2t.py export -d out/orders-first -f csv -o out/orders-first.csv
```

任务按保存的地址请求两页，导出 11 条订单。输出与 `output/expected.csv` 一致。
页面导航文字保留在未识别内容中，表格行按字段规则解析。

再次执行同一张任务卡，保存到新的数据集：

```powershell
python _dev/c2t.py task run -p out/orders.toml -d out/orders-repeat --yes
python _dev/c2t.py export -d out/orders-repeat -f csv -o out/orders-repeat.csv
python _dev/c2t.py task show out/orders.toml
```

两次执行使用相同规则，两个导出文件均对应独立答案。任务记录包含运行次数、每次耗时、首次配置至确认耗时、修正次数和成功重跑次数。再次独立采集使用新数据集；续采使用原数据集。

## 预览与纠错

```powershell
python _dev/c2t.py task check out/orders.toml --text-file scenes/order-ledger/input/page-2.txt --field 金额 --lines 10
```

预览显示字段值、来源行号、填充率和规则偏离。需要提取金额中的数字时，先查看整批效果，再保存：

```powershell
python _dev/c2t.py task fix out/orders.toml --field 金额 --kind search --pattern '¥\s*(?P<v>[0-9,]+\.[0-9]{2})' --text-file scenes/order-ledger/input/page-2.txt
python _dev/c2t.py task fix out/orders.toml --field 金额 --kind search --pattern '¥\s*(?P<v>[0-9,]+\.[0-9]{2})' --text-file scenes/order-ledger/input/page-2.txt --write
```

此修正去掉金额中的货币符号，适用于数字金额输出；独立答案对应原始规则。修改后的规则可在新数据集运行。分段纠错和原文标记见 [使用说明](../../docs/usage.md#按原文标记记录和字段)。

## 接入已登录后台

先启用浏览器调试端口并登录后台，用 `clip2table doctor` 查看连接状态。将后台第一页表格复制为 `page.txt`，填写实际地址与下一页按钮的 CSS 选择器：

```powershell
clip2table task new --text-file page.txt --channel browser --source 'https://your-site.example/orders' --next 'a.next' --max-pages 2 --out orders.toml
clip2table task check orders.toml --text-file page.txt --lines 30
clip2table task check orders.toml --text-file page.txt --confirm
clip2table task run -p orders.toml -d out/backend-first --yes
clip2table export -d out/backend-first -f xlsx -o orders.xlsx
```

向导收集字段名、复合主键、来源和翻页配置。浏览器使用已有登录会话并按 URL 定位标签页；按钮翻页从当前页面开始，独立重跑前将页面切回第一页。后台支持页码 URL 时，使用含 `{page}` 的 `--source`，任务会自动定位第一页并逐页导航。

本地 HTTP 回归测试执行真实请求、导出比对和二次运行；浏览器导航回归测试使用模拟 CDP 客户端。
