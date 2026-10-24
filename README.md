# clip2table

clip2table 是一个面向 Windows 的文本转表格工具。它从网页、桌面应用、剪贴板或本地文件读取可复制的文本，识别记录和字段，导出 CSV、XLSX、JSONL 和 Markdown。

[![Release](https://img.shields.io/github/v/release/yjq123-zyh/clip2table)](https://github.com/yjq123-zyh/clip2table/releases/latest)
[![tests](https://github.com/yjq123-zyh/clip2table/actions/workflows/tests.yml/badge.svg)](https://github.com/yjq123-zyh/clip2table/actions/workflows/tests.yml)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows-lightgrey)](#环境要求)

可用于整理后台订单、公告列表、招聘信息、商品评价和本地台账。重复采集时，可以用 TOML 卡片保存字段规则和翻页方式，并将结果累积到同一个数据集。

## 功能

- **结构识别**：识别表格、周期记录、键值对和编号列表，展示分段依据及候选结构。
- **规则卡片**：用 TOML 配置采集通道、记录边界、字段提取、去重和翻页。
- **批量采集**：支持分页、数据累积和按通道恢复采集位置。
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

从 [GitHub Releases](https://github.com/yjq123-zyh/clip2table/releases/latest) 下载 Python 安装包，或直接安装 `v0.1.0`：

```powershell
python -m pip install https://github.com/yjq123-zyh/clip2table/releases/download/v0.1.0/clip2table-0.1.0-py3-none-any.whl
```

从源码安装：

```powershell
git clone https://github.com/yjq123-zyh/clip2table.git
cd clip2table
python -m pip install -e .
```

## 使用

复制需要整理的文本后，查看结构识别结果：

```powershell
clip2table infer
```

读取剪贴板并导出 CSV：

```powershell
clip2table run -o result.csv
```

读取网页并预览前三条记录：

```powershell
clip2table run --url https://example.com/list --show 3
```

生成规则卡片：

```powershell
clip2table infer --write-card mysite.toml
```

调整卡片中的目标地址或窗口、字段和翻页规则后，执行分页采集：

```powershell
clip2table harvest -p mysite.toml --max-pages 20 --yes -f csv -f xlsx --dir out
```

内置卡片覆盖公告、后台表格、内容块、招聘信息和编号列表，可用作配置参考。完整参数和操作步骤见 [使用说明](docs/usage.md)。

## 使用条件

采集对象需要提供可选中、可复制的文本。折叠内容应先展开，验证码由用户完成。浏览器最小化时，采集会短暂恢复窗口并归还焦点。

文本结构识别使用启发式规则。页面布局变化后，可结合字段填充率、结构偏离提示和原文调整卡片。原文缺少的字段保留为空，未识别内容保存到配套清单。

## 文档

| 内容 | 链接 |
|---|---|
| 使用方法与卡片配置 | [使用说明](docs/usage.md) |
| 模块职责与数据流 | [架构说明](docs/architecture.md) |
| 设计取舍 | [设计记录](docs/design-notes.md) |
| 验收记录 | [验证记录](docs/verification.md) |
| 版本发布与 PyPI 配置 | [发布说明](docs/publishing.md) |
| Agent 操作流程 | [SKILL.md](SKILL.md) |
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
