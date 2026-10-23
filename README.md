# clip2table

**把屏幕上看得到、选得中的任何内容，变成一张结构化表。**

[![tests](https://github.com/yjq123-zyh/clip2table/actions/workflows/tests.yml/badge.svg)](https://github.com/yjq123-zyh/clip2table/actions/workflows/tests.yml)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows-lightgrey)](#环境要求)

不是爬虫。爬虫要假装自己是浏览器、绕过指纹、维护代理池，然后在你和站点之间
永远打一场你赢不了的军备竞赛。clip2table 换了个思路：**让真浏览器、真程序自己去复制**，
它只负责把复制出来的文本切成表。

所以它对你自己的登录态、付费墙后的后台、只有客户端才能看到的列表，一视同仁——
因为那些东西本来就在用户屏幕上，而用户有权限看。

```
┌ 你的屏幕 ─────────────────────────────┐
│ 一个需要登录的后台订单页              │
│ 一个反爬很凶的招聘站                  │
│ 一个只在 Windows 客户端里存在的台账   │
└───────────────┬───────────────────────┘
                │  自动 Ctrl+A / Ctrl+C（真按键，不是 DOM 抓取）
                ▼
        剪贴板：纯文本 + CF_HTML
                │
                ▼
   sniff → normalize → segment → extract → assemble → export
                │
                ▼
        CSV / Markdown / JSONL / XLSX
```

---

## 看一眼真实输出

不配任何规则，直接把一段乱糟糟的复制文本丢给它：

```
$ clip2table infer --text-file fixtures/sample-ecom.txt --show 1
file ｜ FILE ｜ S1 ｜ 3 行 × 5 列 ｜ 未识别 0 行
来源：未知（file 没有提供可核对的地址）
字段填充：line1 100% ｜ 日期 100% ｜ line3 100% ｜ 规格 100% ｜ line5 100%
✓ 行数守恒：15 行全部有归属（记录占用 15 行 + 未识别 0 行）
· S1：按周期锚点切成 3 条记录（每 5 行一条）
· 列名按每列的形态推断，键值列已去掉「键：」前缀，可在卡里改成业务名

前 1 条：
  1. line1=星海无线耳机 Pro ｜ 日期=2026-03-14 ｜ line3=包装完好，降噪比上一代强不少，通勤地铁里基本听不到报站 ｜ 规格=黑色 / 降噪版 ｜ line5=有用(128)

依据：
  · 周期候选 1：每 5 行一条，从第 1 行起，3 条；边界一致 100%，最弱一位的结构占比 100%，完全同形的记录 3/3 条
```

注意最后那段「依据」。它不是在炫技——**它把「为什么认为这是 3 行 × 5 列」摊开给你看**，
你才有机会说"你猜错了"。只给结论不给依据的自动推断，是最难发现错误的那种。

---

## 装

```bash
pip install clip2table
clip2table doctor       # 自检：剪贴板 / 卡目录 / 浏览器调试端口
```

零必需依赖，Python 3.11+，Windows 10/11。

要给这个项目改代码，从源码装：

```bash
git clone https://github.com/yjq123-zyh/clip2table.git
cd clip2table
pip install -e .
```

完全不想装包也可以：`python _dev/c2t.py <子命令>`，效果一样（它只是把 `src/` 挂上 `sys.path`）。

## 三条命令

```bash
clip2table infer                                        # 读剪贴板，反推结构，先看清楚再存
clip2table run --url https://example.com/list --show 3  # 自动选通道取一页，打印报告
clip2table harvest -p mysite.toml --max-pages 20 --yes -f csv -f xlsx --dir out
```

`harvest` 是主线：按卡里的规则翻页、累积、去重、断点续采。卡怎么写、累积采集、
断点续采、安全边界，全在 **[docs/usage.md](docs/usage.md)**。

---

## 三条不许

这是这个项目存在的理由，也是它跟"剪贴板转表格小工具"的分界线。

**1. 不许静默丢行。** 每条源文本要么进了某条记录，要么进了未识别清单，两者之和必须等于源行数。

**2. 不许把切错的表当好表。** 行数守恒只能证明"没丢"，证明不了"切对了"。
所以还有独立答案键回归，以及卡里 `record_lines` / `required` 的逐页形状校验。

**3. 不许补值。** 表里没有的值就是空。空缺率本身就是信息——填平了就再也看不出来了。

---

## 三条通道

| 通道 | 引擎 | 用在哪 |
|---|---|---|
| `http` | HTTP 直连 | 页面服务端就渲染好了 |
| `browser` | E1：CDP 发真 Ctrl+A/Ctrl+C | 要登录、要 JS、有反爬 |
| `desktop` | E2：Win32 `SendInput` | 目标不是浏览器：客户端、Excel、记事本 |

不指定时自动降级，**报告第一行永远写清楚这次走了哪条、为什么**。

---

## 能做什么 / 不能做什么

| 能做 | 说明 |
|---|---|
| 需要登录的后台列表 | 用你自己的浏览器、你自己的会话，不碰你的 Cookie |
| 反爬很凶的站点 | 请求是浏览器自己发的，指纹、IP、UA 全真 |
| Windows 客户端里的台账 | 只要能 Ctrl+A / Ctrl+C，就能进表 |
| Excel 选中区域 | 桌面通道直接读，含 CF_HTML 的表格结构 |

| 不能做 | 为什么 |
|---|---|
| 图片、Canvas 里的文字 | 它不在文本流里，复制不出来 |
| 要逐个点开才能看到的内容 | 卡里能写少量准备动作，复杂交互做不了 |
| 有验证码的页面 | 会停下来报错，不绕过。验证码该由人来过 |
| 浏览器窗口最小化时的无痛复制 | Chromium 不允许隐藏标签页写剪贴板，会短暂借前台再还回去 |
| 非 Windows | 通道 C 直接调 Win32 API，通道 B 靠窗口句柄还焦点 |

还有一条要说清楚：**纯文本切分是启发式的**。页面结构变了就会切错——所以才有行形状校验、
独立答案键回归、逐页偏离标记。它不能保证永远切对，它能保证**切错了你不会不知道**。

---

## 文档地图

| 想知道 | 看这里 |
|---|---|
| 现在能做什么、怎么用、卡怎么写 | **[docs/usage.md](docs/usage.md)** |
| 代码怎么组织、数据怎么流、状态存在哪 | **[docs/architecture.md](docs/architecture.md)** |
| 为什么这么设计、哪些方案被否掉了 | **[docs/design-notes.md](docs/design-notes.md)** |
| 十六项整改各自的验收证据 | [docs/verification.md](docs/verification.md) |
| 怎么发版、PyPI 怎么配 | [docs/publishing.md](docs/publishing.md) |
| 给 agent 用的决策循环 | [SKILL.md](SKILL.md) |
| 改了什么 | [CHANGELOG.md](CHANGELOG.md) |
| 权利归属、能拿去做什么、免责 | [NOTICE.md](NOTICE.md) |
| 历史方案、实验记录、验收过程 | [docs/history/](docs/history/) —— **不代表当前能力** |

---

## 开发

```bash
pip install -e .
python -m unittest discover -s tests -t .     # 472 项
python _dev/c2t.py selftest                   # 文档里的命令与真实参数表对账
python _dev/c2t.py test notice-list           # 内置卡的逐字节回归
```

`SKILL.md` 是给 agent 看的决策循环，不是参数手册——参数手册是 `--help` 的活。
它里面出现的每条子命令都会被 `selftest` 拿去和真实 argparser 比对，写错了就非 0 退出。
也就是说，**文档里写了不存在的命令，CI 会红**。它校验的是仓库里的文档，所以要在这个
目录里跑；装好的副本里没有文档，它会报错退出而不是假装通过。

欢迎 issue 和 PR。提 PR 前请确认 `unittest` 与 `selftest` 都是绿的；
改内置卡或 fixtures 请连带更新 `.expect.tsv`，那是逐字节比对的基准。

## 环境要求

Windows 10/11、Python 3.11+。通道 B 需要浏览器带 `--remote-debugging-port` 启动，
`clip2table doctor` 会给你一行可以直接粘贴的命令。

## 许可

MIT，见 [LICENSE](LICENSE)。

著作权归属、授权范围、禁止性规定、免责与争议解决见 [NOTICE.md](NOTICE.md)。
软件许可的正式条款以 MIT 许可证为准；两者不一致时以 MIT 为准。
