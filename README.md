# clip2table

clip2table 是一个面向 Windows 的文本转表格工具。它从网页、桌面应用、剪贴板或本地文件读取可复制的文本，识别记录和字段，导出 CSV、XLSX、JSONL 和 Markdown。

[![Release](https://img.shields.io/github/v/release/yjq123-zyh/clip2table)](https://github.com/yjq123-zyh/clip2table/releases/latest)
[![tests](https://github.com/yjq123-zyh/clip2table/actions/workflows/tests.yml/badge.svg)](https://github.com/yjq123-zyh/clip2table/actions/workflows/tests.yml)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows-lightgrey)](#环境要求)

可用于整理后台订单、公告列表、招聘信息、商品评价和本地台账。重复采集时，可以用 TOML 卡片保存字段规则和翻页方式，并将结果累积到同一个数据集。同一批业务数据被观察多次时，可以按业务时间判断哪一次算当前状态，并导出变化与待处理。

## 功能

- **结构识别**：识别表格、周期记录、键值对和编号列表，展示分段依据及候选结构。
- **任务向导**：从样本配置字段、复合主键、采集来源与翻页，保存为可重复执行的任务。
- **预览纠错**：定位字段来源、选择并保存分段、修正规则、确认样本，记录配置耗时与运行情况。
- **规则卡片**：用 TOML 配置采集通道、记录边界、字段提取、去重和翻页。
- **批量采集**：支持分页、数据累积和按通道恢复采集位置。
- **规则重放**：改规则后用保存的原文重新解析，不重新采集；差异定位到原文行，确认后再切换。
- **台账视图**：按任务声明的业务时间取当前值，导出当前状态、变化、历史与待处理；旧版本晚到不覆盖新的，缺席不当作删除。
- **程序接口**：`clip2table.api` 提供版本化请求与结构化结果，状态与需要用户处理的步骤可被程序识别。
- **运行恢复**：保存原运行配置和已提交进度；通过请求标识重试，或继续原运行并回答待处理动作。
- **数据记录**：保存原文、字段来源、解析状态、重复记录和内容冲突。
- **格式导出**：输出 CSV、XLSX、JSONL 和 Markdown，超长内容保存到配套文件。

## 工作方式

| 通道 | 取得内容的方式 | 适用场景 |
|---|---|---|
| `http` | 直接请求网页 | 服务端生成的页面 |
| `browser` | 通过 CDP 在浏览器中执行复制 | 使用现有登录会话的网页、动态列表 |
| `desktop` | 通过 Win32 按键复制窗口内容 | Windows 客户端、Excel、文本窗口 |

也可以直接读取剪贴板或本地文本、HTML 文件。取得内容后，程序按以下流程处理：

```text
读取内容 → 识别结构 → 划分记录 → 提取字段 → 去重与保存 → 导出表格
```

## 环境要求

- Windows 10/11。
- Python 3.11 或以上版本。
- 运行时使用 Python 标准库，零必需第三方依赖。
- 浏览器通道需要启用远程调试端口，`clip2table doctor` 可显示启动方法。

## 安装

从 [GitHub Releases](https://github.com/yjq123-zyh/clip2table/releases/latest) 下载 Python 安装包，或直接安装 `v0.3.0`：

```powershell
python -m pip install --upgrade clip2table==0.3.0
```

也可以下载 [v0.3.0 wheel](https://github.com/yjq123-zyh/clip2table/releases/download/v0.3.0/clip2table-0.3.0-py3-none-any.whl)。

从源码安装：

```powershell
git clone https://github.com/yjq123-zyh/clip2table.git
cd clip2table
python -m pip install -e .
```

## 使用

从样本创建任务，按提示填写字段名、主键、通道、来源和翻页方式：

```powershell
clip2table task new --text-file page.txt --out mysite.toml
```

预览字段与原文，确认后记录结果：

```powershell
clip2table task check mysite.toml --text-file page.txt --lines 30
clip2table task check mysite.toml --text-file page.txt --confirm
```

表格、字段的来源行、原文每行归哪条记录会一起打出来。字段取错行时改一条规则，
先看整批效果，确认后再存：

```powershell
clip2table task fix mysite.toml --field 金额 --from-line 5 --text-file page.txt
clip2table task fix mysite.toml --field 金额 --from-line 5 --text-file page.txt --write
```

这里的 `5` 是预览显示的原文行号。也可以用 `--value '¥100.00'` 定位第一条记录中的
取值行，或使用正则提取行中的部分内容：

```powershell
clip2table task fix mysite.toml --field 金额 --kind search --pattern '¥\s*(?P<v>[0-9,]+\.[0-9]{2})' --text-file page.txt
clip2table task fix mysite.toml --field 金额 --kind search --pattern '...' --text-file page.txt --write
```

分段需要调整时，可用 `task check ... --pick N` 预览编号候选，加 `--write` 保存；创建任务时用
`--record-start 3 --record-end 6` 标记首条完整记录的边界。操作示例见
[按原文标记记录和字段](docs/usage.md#按原文标记记录和字段)。

之后每次要新数据，一条命令接着采：

```powershell
clip2table task run -p mysite.toml -d out/orders --max-pages 20 --yes
clip2table export -d out/orders -f csv -f xlsx --dir out
```

重复请求可指定 `--request-id orders-20261028`；中断后使用返回的运行标识继续：

```powershell
clip2table task run -d out/orders --resume run-xxxx --request-id orders-continue --json
```

新一次采集追加独立观察，历史保留每次来源；恢复使用原配置。动作回答和 Python 用法见[结果版本、持续台账与恢复](docs/usage.md#10-结果版本持续台账与恢复)。

`task show mysite.toml` 会告诉你这个任务跑过几次、首次耗时多少、改过几次规则、
其中几次重跑不用再改。完整参数和操作步骤见 [使用说明](docs/usage.md)；
一个从头到尾走通的例子在 [scenes/order-ledger](scenes/order-ledger/)。

### 规则改了、数据又观察了一次

规则写错不用重新抓页面——用存下来的原文重读：

```powershell
clip2table replay -d out/orders -p edited.toml --show 10   # 用改过的规则重算，看差异
clip2table replay -d out/orders -p edited.toml --select    # 确认无误，切成当前台账
```

同一批订单被观察第二次时，按任务声明的业务时间判断哪一次算当前状态：

```powershell
clip2table rev list -d out/orders                                    # 这个目录有哪些结果版本
clip2table ledger -d out/orders --view current                       # 当前台账
clip2table ledger -d out/orders --view changes --baseline v-xxxx     # 相对某版变了什么
clip2table ledger -d out/orders --view pending                       # 需要人判断的
clip2table ledger -d out/orders --view current -f xlsx -o out/now.xlsx
```

`[ledger] sort = "business_time"` 时，三天前才被读到的旧通知仍算旧——**旧版本晚到不会覆盖新的**。
两次观察范围不一样时，没被重新观察到的订单按原样保留，**缺席不当作删除**。

**已经有卡**，也可以直接走底层命令：

```powershell
clip2table infer                                    # 看剪贴板里的文本是什么结构
clip2table run -o result.csv                        # 剪贴板 → CSV
clip2table run --url https://example.com/list --show 3
clip2table harvest -p mysite.toml --max-pages 20 --yes -f csv -f xlsx --dir out
```

内置卡片覆盖公告、后台表格、内容块、招聘信息和编号列表，可用作配置参考。

## 使用条件

采集对象需要提供可选中、可复制的文本。折叠内容应先展开，验证码由用户完成。浏览器最小化时，采集会短暂恢复窗口并归还焦点。

文本结构识别使用启发式规则。页面布局变化后，可结合字段填充率、结构偏离提示和原文调整卡片。原文缺少的字段保留为空，未识别内容保存到配套清单。

## 文档

| 内容 | 链接 |
|---|---|
| 使用方法、卡片与任务配置 | [使用说明](docs/usage.md) |
| 模块职责与数据流 | [架构说明](docs/architecture.md) |
| 设计取舍 | [设计记录](docs/design-notes.md) |
| 验收记录 | [验证记录](docs/verification.md) |
| 版本发布与 PyPI 配置 | [发布说明](docs/publishing.md) |
| Agent 操作流程 | [SKILL.md](SKILL.md) |
| 完整走通的例子 | [订单台账场景包](scenes/order-ledger/) |
| 版本变化 | [CHANGELOG.md](CHANGELOG.md) |
| 著作权、授权与使用责任 | [NOTICE.md](NOTICE.md) |
| 历史材料 | [docs/history](docs/history/) |

## 开发

```powershell
python -m unittest discover -s tests -t .
python _dev/c2t.py selftest
python _dev/c2t.py test notice-list
```

`selftest` 在源码仓库中核对文档中的命令；`test` 将卡片样本与独立答案文件逐字段比较。修改卡片或样本时，请同步更新对应的 `.expect.tsv`。

## 许可

本项目采用 [MIT 许可证](LICENSE)。著作权归属、授权范围与使用责任见 [NOTICE.md](NOTICE.md)。
