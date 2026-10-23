# 验收记录（M1–M4）

> **历史材料，不代表当前能力。**
> 本文件记录的是当时的方案 / 实验 / 验收过程，保留是为了追溯"为什么变成现在这样"。
> 想知道这个工具现在能做什么、怎么用，请看 `docs/usage.md`；想知道代码怎么组织，看 `docs/architecture.md`。
> 文中出现的命令、参数、文件名可能已经改名或废弃，**不要照抄**。

每条都是本机真实跑出来的输出，不是设计意图。命令、输出关键行、以及「这条证明了什么」三件一起写。
跑测试：`py -3 -m unittest discover -s tests -t .` → **292 tests OK**。

---

## M1 剪贴板闭环（6/6）

| # | 验收项 | 结果 | 证据 |
|---|---|---|---|
| 1 | 从 Excel 复制 3×10 → 无参 `run` → 10 行 3 列逐字符相等 | PASS | `_probe/m1_1_excel_copy.py`：Excel COM `Range("A1:C11").Copy()`，`text len=392`、`text == expected (raw CRLF)? True`；CLI `desktop ｜ CLIP ｜ S0 ｜ 10 行 × 3 列 ｜ 未识别 0 行`，三字段 100%，守恒 `11 行全部有归属` |
| 2 | 只有 CF_HTML 没有纯文本时仍能切出列 | PASS | `_probe/m1_2_html_only.py` 只写 `HTML Format`（`has CF_UNICODETEXT: False`）→ `S0 ｜ 3 行 × 4 列`，四字段 100% |
| 3 | 三类版式各出一张表 | PASS | `infer --text-file`：`sample-ecom.txt` S1 3×5、`sample-gov.txt` S1 3×6（含 法定代表人/登记状态/注册资本）、`sample-hr.txt` S1 4×5，均未识别 0 行 |
| 4 | 空剪贴板 → 退出 2、一句人话、无 traceback | PASS | `_probe/m1_456.py` 9/9：`✗ 剪贴板是空的，或者里面没有文字` / `  先在目标页面/程序里 Ctrl+A、Ctrl+C，然后再跑一次`，退出码 2 |
| 5 | `--dry-run` 前后目录文件数不变 | PASS | 同脚本：`31 -> 31`，`-o out/zzz.csv` 确认没生成 |
| 6 | 读剪贴板前后内容与序列号都不变 | PASS | 同脚本：序列号 `7645 -> 7645`，30 字符逐字符不变；探针结尾把剪贴板还原成原状 |

---

## M2 浏览器自动按键（7/7）

| # | 验收项 | 结果 | 证据 |
|---|---|---|---|
| 1 | 真实 Ctrl+C 与页面一致 | PASS | `_probe/m2_1_dom_crosscheck.py`：`harvest exit=0`、12 行 × 4 列、`DOM rows = 12`、`CSV rows = 12`、`mismatches: 0`。对照值走 CDP 直接从 DOM 取，**不经过剪贴板路径**，避免自证 |
| 2 | 零字段选择器从真实站点取 ≥20 行 | PASS | `books.toscrape.com` 全选复制，卡里只有「怎么选中 + 一条记录长什么样」：`browser ｜ E1 ｜ card ｜ 20 行 × 2 列 ｜ 未识别 58 行`，price/stock 全 100% |
| 3 | 自动翻页 3 页且去重后 > 单页 | PASS | `harvest --max-pages 3 --yes`：单页 12 行 → 三页 `36 行 × 4 列 ｜ 3 页`，`每页新增：[12,12,12]`，三页 URL 依次 `/page/1,2,3` |
| 4 | 前台不被打断 | PASS | `_probe/m2_4_cli_acceptance.py` 18/18（三个 CASE，见下节） |
| 5 | `doctor` 给可直接粘贴的启动命令 | PASS | 端口未开时输出 `Start-Process "<真实 exe 路径>" -ArgumentList '--remote-debugging-port=9222', "--user-data-dir=$env:TEMP\c2t-profile-9222"`；探针把这一行真的用 `pwsh -File` 执行，0.8s 后端口应答 |
| 6 | `cards validate` 逐个定位到键 + `test` 报记录号 | PASS | `_probe/m2_2_errors.py` 17/17 命中指定 key（如 `[paging].style 只能是 none/click，实际是 'scroll'`）；`_probe/m2_6_acceptance.py` 证明分工真实：把 `[fields.title].index` 0→1，`validate` 仍 exit 0，`test` exit 2 并列出 `记录 #1 title: 期望 '临江区…' 实际 '2026-01-15'` |
| 7 | 验证码时停下、不重试、不 traceback | PASS | `_probe/m2_challenge_cli.py` 8/8：四个墙（CF interstitial / hCaptcha / Turnstile / geetest）全部 `exit=2 clip_changed=False` 且首行点名是哪种墙；四个对照（右下角 badge、内嵌 Turnstile 登录页、普通登录页、单条记录页）全部 exit=0 |

### M2#4 三个 CASE（一条命题盖不住三种情形）

| CASE | 情形 | 结果 | 实测数字 |
|---|---|---|---|
| 1 | 窗口可见、本标签不是活动标签、用户在别的程序 | 不打扰 | `foreground samples` 全程 distinct=1，`rows=12`，notes 不含「提到前台」 |
| 2 | 浏览器窗口最小化 | 借前台并归还 | `borrowed=True`、`vis hidden → visible`、`rows=12`、还款 `527986 → 199728（prev）` 当场落地、notes 明说「临时提到前台，已把焦点还给原来的窗口」 |
| 3 | 可见性修不回来 | 发键前报错 | 真实发生过 `exit=2 / 标签页仍是隐藏状态`（前台未动）；单测 `test_still_hidden_afterwards_stops_before_any_key` 锁住 |

**为什么 CASE 2 原来判失败，是判定写错了**：原脚本在 CLI 退出后采样前台，量到 explorer/微信，于是一度以为还款失败。插桩 `_give_back_foreground` 后看到真相——还款当场落地（`527986 → 199728`），400ms 后**微信**抢走前台、再之后 **Excel** 抢走。这台机器上有别的程序在随时抢焦点，事后采样量到的是它们，不是我们。

**`Browser.setWindowBounds` 被实测否掉**：在真正修不好可见性的那个状态下，它翻可见 0/5 且照样移动前台；同一轮里 `Page.bringToFront` 5/5。已写进 `_make_visible` 的 docstring，不留「以后试试这个」的悬念。

---

## M3 桌面通道 E2（5/5）

| # | 验收项 | 结果 | 证据 |
|---|---|---|---|
| 1 | 记事本取 ≥20 行 | PASS | `_probe/m3_desktop.py`：`desktop ｜ E2 ｜ card ｜ 22 行 × 4 列 ｜ 未识别 0 行`，四字段 100% |
| 2 | 与手工复制一致 | PASS | 每个字段值必须作为子串出现在手工复制全文里：`values not found verbatim in the manual copy: 0`（逐行比会误判——`01` 出自 `工单第 01 号`） |
| 3 | 目标窗口不存在时报错 | PASS | `exit=2`、`✗ 没有找到窗口 title='这个标题根本不存在-.+' exe='notepad.exe'`、无 traceback |
| 4 | 同一张卡换引擎 | PASS | 把 M2 的 CDP 卡只改 `channel` 与「怎么选中」，切分/字段/dedupe 一行不动：`desktop ｜ E2` 与 `browser ｜ E1` 产出**同样的 12 行 × 4 列** |
| 5 | 三条通道各有一次真实走通 | PASS | `_probe/m3_5_ladder.py` 17/17：A `/plain` → `http ｜ HTTP`（1368 字符）；B `/shell` 需要 JS → A 档拒绝后降级 `browser ｜ E1`（2164 字符，门禁文字消失）；C 掐掉端口 → `desktop ｜ E2`（2164 字符）；三档全废时抛 `ChannelError` 并逐条列出三档原因 |

### M3 修掉的两个真缺陷

- **`line` 字段的 `pattern` 被收下然后静默忽略**：卡里写 `^负责人[:：]\s*(?P<v>.+)$` 想要「甲一」，拿到「负责人：甲一」，而填充率仍 100%。修法是没匹配返回 `None`，**绝不回退到整行**（回退是同一个 bug 的静默版本）。
- **WinUI3 程序焦点停在 `InputSiteWindowClass`**：程序化激活后按键被吞、SendInput 却报成功。实测同窗口下 6 个 `RichEditD2DPT`，只有 `IsWindowVisible` 为真的那个能复制（另外 5 个 `SetFocus` 成功但复制不出来）。`focus_text_control` 只认可见文本控件，多个可见就拒绝猜。

---

## M4 累积采集（7/7）

| # | 验收项 | 结果 | 证据 |
|---|---|---|---|
| 1 | 多页并集 | PASS | `_probe/m4_union_export.py`：逐页独立取数手工求并集 15 条 = dataset 15 行，标题集合**完全相等**（对照不经过 dataset 代码） |
| 2 | 断点续采 | PASS | `_probe/m4_kill_resume.py` 用 `subprocess.kill()` 真杀：杀在第 3 页落盘后 `rows=9 pages_done=3`；重跑 `rc=0` 且打印「从第 4 页继续」；最终 `rows=27 页=[1..9] 唯一 title=27`；raw 9 份；再跑不重复累积 |
| 3 | 循环页自动停 | PASS | `m4-loop --max-pages 9` → `停止原因：连续 2 页没有新增行`、`每页新增：[3,0,0]` |
| 4 | 大表性能 | PASS | 1 万行 5 列：`0.07s / 206 KB`（预算 3s） |
| 5 | 冲突三策略 | PASS | `_probe/m4_collision.py` 6/6：`report_only` 3 行 + 报「冲突 6 行」；`keep_first` 3 行且正文全来自第 1 页；`keep_all` 9 行且 6 行带 `_dup_of` |
| 6 | 5 份内置卡含 ≥2 desktop，各带 samples 且 `test` 通过 | PASS | `notice-list`(browser/anchor)/`admin-order-list`(browser/lines)/`generic-item-block`(desktop/anchor)/`job-list`(desktop/blank)/`numbered-list`(desktop/anchor) —— 3 份 desktop；五份 `test` 全绿（记录数 3/5/3/4/4） |
| 7 | `selftest` 双向 | PASS | 正常 `✓ SKILL.md 里的 7 个子命令全部存在` exit 0；追加一个不存在的子命令 → `✗ SKILL.md 提到不存在的子命令：['nonsense-cmd']` **exit=2** |

### M4 修掉的真缺陷

- **`admin-order-list` 用 `mode="single"` 把整张表塌成 1 行**：`order_no` = 「订单号」（表头文字），amount/status/note 全空，而报告显示 `未识别 0 行`、守恒平衡——**所有信号都说「干净」，整张表却没了**。新增 `mode="lines"` + `skip_header`；`infer --write-card` 对 S0 也发 `single`，同类塌表，一并改掉。
- **`_count_new` 与 dataset 的去重口径不一致**：drift 页报 `每页新增：[3,3,3]` 而同一份报告写 `本轮：新增 3 行` —— 同一个词两个数，其中一个必然是错的。统一按卡的 `dedupe_key` 计数。
- **「续采到底」被当成报错**：采完第 9 页后请求第 10 页拿到 404 → exit 2。新增 `_EndOfList`：列表到底了是停止条件，第 3 页变登录墙才是失败，两者原来文案与退出码都一样。

---

## 收尾修补（不在里程碑里，但都是「报告说做了、实际没做」）

| 缺陷 | 修法 |
|---|---|
| 报告说「未识别的 N 行原文已保留」，导出时根本不留 | 导出时写同名 `.unparsed.txt` 侧车；**故意不做成表里的一列**（32767 单格上限、一段导航文字会毁掉表的形状） |
| `-o out/x.csv -f csv -f xlsx` 时 `-o` 被静默忽略 | 多格式时按 `-o` 换后缀派生 |
| `export` 子命令没有 `--dir` | 补上，与 `run`/`harvest` 一致 |
| `run --text-file` 打出 `file ｜ HTTP` —— 根本没发生 HTTP | 改用 `_engine_label()`：`file ｜ FILE`、剪贴板 `desktop ｜ CLIP`、浏览器 `browser ｜ E1` |

---

## 一条被实测推翻的设计假设（留着，因为它是这个项目最贵的教训）

方案里写「E1 不抢前台，用户可以同时在别的地方干活」。**按键确实能到后台标签页**（keydown 监听器 100% 命中），但 **Chromium 拒绝为隐藏标签页写剪贴板**——于是「不抢前台」这句话在最需要它的场景下失效。

真实定论是三种情形分开的，已同步改进 README / SKILL.md / 方案文档 / `PROBE-RESULTS.md`：
窗口被盖住 → 不打扰（8/8）；窗口最小化 → 必抢前台，还（5/5）；修不好 → 发键前报错。

写这句话的时候我只有一个探针的结论，没测「复制到底成不成」，只测了「按键到底到不到」。**测到的一半不能当初全部结论用。**
