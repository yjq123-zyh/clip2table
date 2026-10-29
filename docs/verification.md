# 验证记录

对应用户先后提出的两批产品评审意见（第 1–8 节、第 10 节各一批），外加第 11 节的三处成本问题。
每条写三件事：**改了什么**、**怎么验**、**验出了什么**。
这里只记当前状态；M1–M4 的历史验收过程在 `history/VERIFY.md`。

环境：Windows 11、Python 3.12.6、零第三方依赖。
发布版本全量（2026-10-29，v0.3.0）：`python -m unittest discover -s tests -t .` → **666 tests OK**，36.243 秒。
`python _dev/c2t.py selftest` → `SKILL.md` + `README.md` + `docs/` 里的 12 个子命令全部存在。

---

## 1. 渠道回退必须保证「取得的是同一个目标」

**改了什么**：`Captured` 拆成 `requested_url`（我要的）与 `observed_url`（实际读到的），
报告第一行写通道 + 引擎 + 判定依据，第二行写来源与实际页面。桌面通道读不到地址栏时，
来源写「未知」而不是拿请求地址冒充。

**怎么验**：

```
$ clip2table run --url https://example.com --show 1
通道 http ｜ 引擎 HTTP ｜ 判定依据：直连就拿到了 577 字符，不需要动浏览器
http ｜ HTTP ｜ S4 ｜ 1 行 × 1 列 ｜ 未识别 0 行
来源：https://example.com
```

**验出了什么**：来源行不再把"请求的地址"当成"读到的地址"。本地文件输入时明确写
`来源：未知（file 没有提供可核对的地址）`——不编。

---

## 2. 续采以「页」为提交单位

**改了什么**：整页先建到 `pages/.tmp-NNNN/`，成功才 `rename` 成 `pages/NNNN/`（不可变）。
`rows.csv` / `state.json` 由已提交页派生，可 `rebuild()`。续采位置按通道分别定义：
`http` 记页号、`browser` 记实际 URL、`desktop` 必须 `--append-from-here`。

**怎么验**：`tests/test_dataset.py`（16 项）覆盖"临时页未发布不算已提交"、
"页目录残缺不读"、"rebuild 与 pages 一致"。

**验出了什么**：不再有"目录说第 3 页完成、里面装第 2 页的行"这种看起来不坏的状态。

---

## 3. 推断要能解释自己，歧义要摆出来

**改了什么**：`infer` 输出「依据」段：每个候选切法的边界一致率、最弱一位的结构占比、
同形记录比例。切法不唯一时并列显示另一种切法长什么样。卡里新增 `record_lines` /
`required`，逐页校验记录形状，偏离**只警告不丢弃**。

**怎么验**（下面两段是节选，只留判据相关的行）：

```
$ clip2table infer --text-file amb.txt
file ｜ FILE ｜ S1 ｜ 3 行 × 3 列 ｜ 未识别 0 行
依据：
  · 周期候选 1：每 3 行一条，从第 1 行起，3 条；边界一致 100%，最弱一位的结构占比 100%，完全同形的记录 3/3 条
```

```
$ clip2table run -p _dev/cards/ecom-strict.toml --text-file _dev/ecom-broken.txt
file ｜ FILE ｜ card ｜ 2 行 × 5 列 ｜ 未识别 0 行
✓ 行数守恒：14 行全部有归属（记录占用 14 行 + 未识别 0 行）
⚠ 结构偏离（卡里声明的记录形态，这一页没满足）：
  · 1/2 条记录超过 5 行（第 1 条）
  这些记录仍然保留并导出；请核对上面几条，或更新卡的约束
```

> 这两份夹具（`_dev/cards/ecom-strict.toml`、`_dev/ecom-broken.txt`）是本地验收用的，
> 没有随仓库发布——它们演示的是「故意做坏一份数据看能不能被抓住」，对使用者没有用。
> 想复现，照第 3 节「卡怎么写」自己写一张带 `record_lines` 约束的卡即可。

**验出了什么**：偏离能被指名道姓地指出，且记录没被删。
`tests/test_structure.py`（8 项）覆盖记录行数越界、必填字段缺失、偏离页写进 `meta.json`。

**顺带修掉的一个报告缺陷**：守恒行原写「记录 {consumed_lines}」，在一条记录占多行时
会读成"14 条记录"，与表头的"2 行"自相矛盾。已改为「记录占用 14 行 + 未识别 0 行」。

---

## 4. 卡声明的东西必须真的能执行

**改了什么**：新增 `capabilities.py` 一张能力表，`cards validate`、实际分发、`cards show`
三处共用。卡里写了当前版本做不到的原语，在校验阶段就拒绝，而不是运行时静默返回空。

**怎么验**：`tests/test_cards.py`（31 项）里 17 类错误卡逐个断言定位到键；
`_probe/m2_2_errors.py` 17/17。

**验出了什么**："页面没有这个元素"、"正则没匹配"、"这个版本根本做不到"三种空值，
现在各自有不同的说法，不再都显示成一个空格。

---

## 5. 字段要能溯源

**改了什么**：每个字段带 `FieldTrace`：类型、来源行号、为什么空（源里没有 / 规则没匹配 /
原语不支持）。报告里有「字段定义」段与填充率，低于 80% 单独拎出来。

**怎么验**：`tests/test_field_trace.py`（33 项）逐条钉住七种状态
（ok / missing / out-of-range / no-match / coerce-failed / unimplemented / rule-error），
断言「空」的四种原因不会互相冒充；真卡输出里每个字段一行 `名字 ｜ 类型 ｜ 规则`。

**验出了什么**：空值不再是"一个空格"，而是可区分的原因。写这组测试时又发现一个真缺陷：
`span` 的边界标记找不到时**退化成整条记录**，于是「商家回复」这一栏在标记消失后
拿到了记录的全部内容——填得满、看着对、是错的。已改为**标记找不到就报空并说明原因**，
并给卡加了显式可选写法 `before?:规格`（在就切在它前面，不在就取到结尾）。
内置卡 `generic-item-block` 原本正是靠那个退化行为才"通过"自己的样本，已一并改正。

---

## 6. 去重 / 内容更新 / 翻页结束是三件事

**改了什么**：冲突拆成**内容变更**（卡的 `identity` 字段没变，只变了 `mutable` 或其它
字段）与**身份冲突**（卡声明的 `identity` 字段变了），两者都写进 `conflicts.json`，
默认都保留**先到**的那条。翻页停止改为末页标记 → 下一页不可用 → 页面指纹循环，
**"连续 N 页无新增"降级为可配置兜底**。dataset 绑定卡快照，改卡后追加需
`--allow-card-change`。

**怎么验**：`tests/test_conflicts.py`（16 项）、`tests/test_paging.py`（11 项）、
`tests/test_card_binding.py`（10 项）。

**验出了什么**："两个人同名"不再被当成"一个人改了名字"；同时纠正了上一版的一个
越权判断——"改了几个字段"被当成身份证据，而数据本身证明不了这件事，现在它只是
`hint`，结论只能由卡声明的 `identity` 给出。

---

## 7. 命令层统一执行流程与写入边界

**改了什么**：新增 `flow.py`，写入边界只有 `Committer` 一处。
命令可以取数、解析、打印，但不许碰数据集。`--dry-run` 不是"提交时跳过"，
而是**根本没有 committer**，所以不可能半写。

**怎么验**：`tests/test_flow.py`（9 项）。修复前 `run -d some/dir` 接受参数、
打印报告、目录从不创建、也不报错——这是被记录在案的真实缺陷。

**验出了什么**：同一个选项在三个入口的含义一致；预览模式不可能写出半个数据集。

---

## 8. 文档三分

**改了什么**：

| 入口 | 文件 | 内容 |
|---|---|---|
| 当前使用说明 | `docs/usage.md` | 真实命令、有效配置、完整使用路径 |
| 架构说明 | `docs/architecture.md` | 模块职责、数据流、续采与持久化 |
| 设计记录 | `docs/design-notes.md` | 关键取舍、实测数字、被替代的方案 |
| 历史材料 | `docs/history/` | 方案 v2、设计评审 v1、探针结果、M1–M4 验收；每份顶部写明"不代表当前能力" |

`README.md` 只留门面 + 文档地图 + 第一次运行；`SKILL.md` 仍是 agent 决策循环，
末尾指向三份文档。

**怎么验**：`selftest` 仍通过（`SKILL.md` + `docs/` 与真实 argparser 对账）；
全量测试 519 项通过（当时是 399 项，后续两轮整改又加了一批）；四份历史文件顶部横幅已加。

**验出了什么**：新读者不用自己追"哪些已实现、哪些已改变、哪些还是预留"。

---

## 9. 写完文档后对着文档反查一遍，又修了六处

文档写完不等于写对。把三份文档里每一条能被机器验证的声明挨个跑一遍，四处是假的；
另外两处是跑测试时暴露的。

| 位置 | 原来的声明 | 实际 | 修法 |
|---|---|---|---|
| `docs/usage.md` | 示例卡里 `[field.商家回复] span = "商家回复"`，标记不存在时退化成整条记录 | 标记找不到时 `span` 拿的是**整条记录**，字段填满了却是错的 | 严格：标记找不到报空（`no-match`）；要"取标记之后的剩余部分"必须显式写 `before?:` |
| `docs/usage.md` | "用 `clip2table cards test 卡.toml` 回归" | 从来没有这个子命令（真命令是顶层 `test`） | 改文档，并把 `selftest` 扩到 `docs/` 且校验两段式子命令 |
| `docs/architecture.md` | 空值原因"分三种" | `FieldTrace.status` 有七种 | 按实际七种重写并给出各自含义 |
| `docs/architecture.md` / `README.md` | 测试 394 项 | 399 项（含本轮新增的 3 项） | 改数字 |
| `docs/usage.md` | （改 `span` 那段时）`const` 一行被挤到表格外面 | 表格断了，`const` 变成正文 | 把 `const` 放回表内 |
| `tests/test_clipboard.py` | 打开剪贴板失败就直接抛 | 别的程序正占用剪贴板时整套测试变红，和被测代码无关 | 有界重试（20×50ms）；`_get_text` 也重试——它静默返回 `""` 会被当成"用户原来的剪贴板"，tearDown 再把这个空串写回去，等于**清空用户的剪贴板** |

**怎么验**：

```
py -3 _dev/c2t.py selftest        # ✓ SKILL.md、README.md、usage.md、architecture.md、design-notes.md
                                  #   里的 9 个子命令全部存在
py -3 -m unittest discover -s tests -t .   # 399 tests OK
```

`selftest` 现在有三条负向测试证明它会失败：编造子命令、两段式编造（`cards test`）、
以及**只在 docs 里**编造。只对账 `SKILL.md` 的那版正是漏掉第三种的版本。

剪贴板重试也有负向测试：连续失败 3 次后成功、以及到达上限必须真的放弃（`attempts=4` 就调 4 次）。
一个"永远重试"的实现会让测试卡死，一个"从不重试"的实现会让测试随机红——两种都要被钉住。

**验出了什么**：文档里写的命令现在要么能跑，要么测试红。上一版是"写了就没人管"。

---

## 10. 第二轮评审的八条

第二轮的意见集中在「数据到了但没落地」：恢复只做了一半、迁移会丢历史、导出会改内容。
八条逐条对照如下。

| # | 问题 | 改了什么 | 怎么验 |
|---|---|---|---|
| 1 | 页已提交、`state.json` 未更新时中断，重开后记录恢复了、位置没恢复；状态文件缺失时页元数据里的字段与冲突也读不回 | 页是唯一真相：打开时先 `_restore_from_pages` 用页元数据填回字段/键/策略/位置，再 `_load_from_pages` 重建行；`state.json` 降级为缓存 | `tests/test_state_recovery.py`（6 项）：删掉 `state.json`、把它写坏、只留 `pages/`，三种情形重开后行数与位置都不变 |
| 2 | `can_restore` 声称「可恢复」，但没有真正执行的恢复能力 | `restore_to()` 按 `[paging].url_template` 导航到第 N 页，然后**读回 `location.href` 与目标比对**；对不上就报错。无法证明的卡（点「下一页」式列表）直接拒绝，并提示用 `--append-from-here` | `tests/test_resume_restore.py`（11 项）：能恢复、不能恢复、导航到错页、地址栏读不到四条路径 |
| 3 | 只有 `rows.csv` 的旧数据集，第一次追加后历史行在重建时消失 | 追加前先把旧 `rows.csv` 冻结成不可变的「历史基线页」（page 0，`raw_available: false`，不伪造原文），之后按「基线 + 各页」重建 | `tests/test_legacy_baseline.py`（6 项） |
| 4 | `html_to_lines` 对每个单元格 `.strip()`，行首/行尾的空单元格被吃掉，整行列位置左移 | 表格按二维网格解析，空单元格保留为占位，不再被首尾裁剪；单元格内 `<br>` 转成单元格内换行 | `tests/test_htmltable.py`（12 项） |
| 5 | 数值转换取正则第一个片段：`10–20`→10、`1.5万`→1.5、`1e3`→1，转换后还被当成成功 | 拆成「抽取原文 / 转换数值 / 分行读取」三件事；转换要求整串匹配（区间、单位、千分位、百分号不再猜），日期用 `datetime.date` 验真 | `tests/test_extract.py`（40 项，含区间、单位、千分位、非法日期各一组） |
| 6 | 键有效性用 `any(...)` 判断，`公司=空` 也算有效键；字段一变就报冲突，分不清「同一实体更新」和「键错了」 | 键默认要全部非空才算有效，否则标 `_identity=未确定`；卡可声明 `identity` / `mutable` 字段；冲突按四类判定（内容更新 / 身份冲突 / 重复 / 无法判定）并给 hint；`keep_all` 只保留不同版本 | `tests/test_conflicts.py`（16 项）、`tests/test_card_binding.py`（14 项） |
| 7 | 卡指纹只覆盖部分配置，改 `line.pattern` 或 `skip_header` 不改指纹，旧数据被静默重解 | 指纹覆盖全部影响解析结果的配置（字段规格、分段、去重、翻页）；每页存卡快照；卡变了拒绝继续，要显式 `--allow-card-change` | `tests/test_card_binding.py`（14 项） |
| 8 | xlsx 直接截断超长单元格、删非法字符且不报；markdown 表头不转义，字段名含 `\|` 就把表撑坏 | 渲染返回 `Change` 明细（行、字段、原因）；超长值另写配套 `.full.jsonl` 保留原文；markdown 表头与正文用同一套转义 | `tests/test_render_lossless.py`（8 项）+ 真 CLI 探针 `_probe/item8_export.py` |

**怎么验**：全量 **519 tests OK**。第 8 条另有真 CLI 证据——
用一个 40000 字符的单元格跑 `run -o out.xlsx`，配套 `out.full.jsonl` 里该值 40000 字符原样完整；
字段名写成 `名|称` 导出 markdown，表头是 `名\|称`，列数仍为 2。

---

## 11. 三处随页数放大的成本

这三处不影响正确性，但页数一多就会拖垮一次采集。

| 位置 | 原来的成本 | 改了什么 | 实测 |
|---|---|---|---|
| `dataset.py` 页级查重 | 每提交一页，把**所有已提交页**的 `raw.txt` 读一遍再算 sha256，N 页共读 N²/2 份 | 每页 `meta.json` 本来就存了 `sha256`，改为开目录时一次性建「页号 → 哈希」缓存，提交后增量更新 | 连续 8 页：每页读 `raw.txt` **0 份**（改前分别是 0/1/2/…/7） |
| `dataset.py` 派生视图 | 每页提交都整表重写 `rows.csv`，哪怕这一页一行新数据都没有 | 加 `_rows_dirty`：只有行真的变动才重写；`rebuild()` 强制重写，因为视图是派生的、必须能从页恢复 | 原文是新的但行全重复的页：`rows.csv` 重写 **0 次**（改前 1 次），恢复位置照常前移 |
| `pipeline/extract.py` 溯源行号 | `scope="tail"` 的字段在切片上 `enumerate`，报出的行号永远是 0..window-1，读起来像「值来自记录开头」 | `_candidates()` 返回（起始偏移, 行列表），行号 = 偏移 + 序号 | 5 行记录、`window=1` 命中末行 → 行号 **4**；`window=2` 命中倒数第二行 → 行号 **3** |

**怎么验**：`tests/test_page_dedupe_cache.py`（5 项）用「数调用次数」而不是计时来钉住前两条；
`tests/test_extract.py` 里两个新用例钉住行号。手工版：`_probe/opt_a_verify.py`。

---

## 12. 发布准备阶段自查出的两条

这两条都是「准备上传 GitHub」时对着装好的副本实跑才暴露的，单测和本机跑法都碰不到。

### 12.1 `selftest` 在装好的副本里「跳过即通过」

`clip2table selftest` 从 wheel 安装的副本运行时，扫到 0 份文档，打印
`· 没找到任何文档，跳过对账`，然后**退出码 0**。

wheel 里只有 `src/`，没有 `SKILL.md` 和 `docs/`，所以这个分支在装机场景下必然命中——
等于这个检查在它唯一可能被外部用户触发的场合，永远只会发绿灯。这恰恰是这个命令存在的
理由（文档里写了不存在的命令要让 CI 红），它自己犯了同一个错。

**改了什么**：`src/clip2table/cli.py` 的 `cmd_selftest` 在 `scanned` 为空时打印
`✗ 没有找到任何可以核对的文档，selftest 什么也没验证`、列出找过的五个路径、
说明「装好的副本里没有这些文档，所以这里不能算通过」，返回 `BAD_INPUT`(2)。

**怎么验**：
- `tests/test_selftest.py` 新增 `TestNothingToCheckIsAFailure` 2 项：路径列表为空 → 非 0；
  路径指向一个空临时目录（模拟装机）→ 非 0 且报出找过的文件名。
- 仓库根：`py -3 _dev/c2t.py selftest` → `✓ …9 个子命令全部存在`，退出 0。
- 装好的副本（`/tmp/cleanvenv` 从源码 `pip install .`）：`clip2table selftest` → 退出 **2**，
  并打印出 `…\cleanvenv\Lib\SKILL.md` 等实际找过的路径。
- CI 加了对应一步：`.github/workflows/tests.yml` 的 `package` 作业里，
  `& $exe selftest` 之后 `if ($LASTEXITCODE -eq 0) { exit 1 }`。

### 12.2 sdist 缺 `fixtures/`，下载源码包的人跑不了测试

`python -m build --sdist` 出的包只有 `src/`(70) + `tests/`(35) + 根文件，`fixtures/` 一份没有。
`tests/` 里有若干用例直接读 `fixtures/sample-*.txt`，于是在解压出来的 sdist 里跑
`python -m unittest discover -s tests -t .` 会红在找不到夹具——代码没坏，包缺料。

**改了什么**：新建 `MANIFEST.in`，把 `fixtures/`、`tests/`、`docs/*.md`、`SKILL.md`、
`CHANGELOG.md`、`_dev/c2t.py` 显式纳入 sdist。wheel 不动（wheel 是给用户的，只该有
运行所需的东西；sdist 是给改代码的人的，两者要的内容不一样）。

**怎么验**（全新 venv `/tmp/sdvenv`，先 `tar xzf` 再 `pip install .`）：
- sdist 顶层现在有 `SKILL.md README.md CHANGELOG.md MANIFEST.in`，目录含
  `docs/`(10) `fixtures/`(9) `_dev/`(2)。
- 解压后 `python -m unittest discover -s tests -t .` → **519 tests OK**。
- 解压后 `python _dev/c2t.py selftest` → 退出 0（`✓ …9 个子命令全部存在`）；
  同一 venv 里用装好的 `clip2table.exe selftest` → 退出 2，正是 12.1 要求的两种结果。

---

## 13. 任务创建、纠错与重复采集（2026-10-26）

验证命令：

```powershell
python -m unittest tests.test_marks tests.test_taskflow tests.test_scene tests.test_resume_restore tests.test_paging
python -m unittest discover -s tests -t .
```

定向验证覆盖任务、分段、场景和分页，并补充浏览器来源替换用例；最终全量 539 项通过，耗时 11.856 秒。

| 路径 | 验证结果 |
| --- | --- |
| 规则持久化 | 字段正则与换算参数保存后重读一致；候选分段保存后，记录数、值与来源行号和预览一致 |
| 卡片转换 | 复合主键、冲突策略、身份与可变字段保留，字段重命名同步引用 |
| 来源配置 | 向导生成 HTTP 页码模板、浏览器目标与按钮选择器、桌面目标与翻页按键 |
| 结果确认 | 确认绑定规则与样本；改变任一项后确认状态更新；首次配置耗时保留首次值 |
| 质量与运行记录 | 展示填充率、必填字段缺失和记录长度偏离；失败运行计入统计；JSON 输出可直接解析 |
| 两页采集与再次执行 | 本地 HTTP 服务返回两个 HTML 表格页面；同一任务卡分别采集到两个新数据集，两个导出文件均与手工答案逐字节一致（11 条订单） |
| HTML 来源定位 | 导航文字保留为未识别内容，字段来源保持对应原文行 |
| 浏览器页码导航 | 模拟 CDP 客户端验证从第一页开始、导航第二页和独立重跑 |

订单场景的 HTTP 请求使用实际本地服务；浏览器翻页测试使用模拟客户端。测试均使用临时任务副本和数据目录。
详细操作见 [订单台账示例](../scenes/order-ledger/README.md)。

发布检查：`python _dev/check_release_workflow.py` 读取当前版本 v0.2.0，匹配标签退出 0、
错误标签退出 1、手动触发退出 0。GitHub Release 与 PyPI 发布成功。

---

## 14. 结果版本、持续台账与恢复（2026-10-28）

当前本地代码全量 666 项通过，新增的操作恢复模块包含 18 项回归用例。测试使用临时数据集；
源码目录中的场景卡片、任务记录及原始输入由测试副本隔离。

```powershell
python -m unittest tests.test_operation_recovery tests.test_revisions tests.test_replay tests.test_ledger tests.test_api tests.test_flow tests.test_scene tests.test_resume_restore
python -m unittest discover -s tests -t .
python _dev/c2t.py selftest
```

| 行为 | 实际覆盖 |
|---|---|
| 跨运行保留观察 | 两个独立 run 都采 page=1；五个实体、八次历史观察，当前值包含更新、晚到、缺席与冲突 |
| 同请求重试 | 相同 request_id 返回原结果；删除回执后仍复用操作记录；输入文件或卡片内容改变时报 request_conflict |
| 来源第一页 | 本地真实 HTTP 服务执行两次独立采集；请求均从 page=1 开始，保留四次行观察并得到三个实体 |
| 原运行恢复 | 原文保存后解析失败，修改源文件与传入卡，再恢复仍使用原始字节和冻结规则；run_id / operation_id 保持一致 |
| CLI 恢复 | 删除原任务卡后通过 run_id 恢复，统计仍是一条业务运行 |
| 重算失败 | 逐捕获失败清单持久化；失败候选无法启用；恢复只处理失败捕获，完整捕获集合保留 |
| 解析差异 | 字段值、字段移除、原文坐标及拆分/合并依据；相同捕获以完整坐标辅助匹配 |
| 动作处理 | 合法 needs_action 正向返回并序列化；错误回答不消耗原动作；正确回答继续原运行 |
| 人工决定 | 按候选 ID 保存决定，重新构建视图仍生效；重复回答复用结果 |
| 取消和计数 | 已提交页后 KeyboardInterrupt 返回 cancelled；累计提交与本次调用新增提交分别统计 |
| 写入与选择 | 独立子进程争用同一写锁被拒绝；相同 revision 再次选择也改变代次，旧 token 不能覆盖新选择 |
| 类型化主键 | 前导零、复合键边界、超出浮点精确范围的整数，以及不符合整数定义的键值 |
| 旧数据 | 缺少原文的第 0 页保留原始行，解析器标为 unknown；源目录字节不变，重算明确列出失败页 |
| 导出恢复 | XLSX 文件落地后模拟结果保存失败，重试复用原文件；摘要和修改时间一致 |
| 导出完整值 | 33000 字符单元格产生损失记录和完整值伴随文件，完整内容可读 |
| CLI/API 共用 | CLI 重算、版本选择、台账导出调用相同应用入口；CSV 与程序导出逐字节一致 |
| 场景答案 | 三页场景的 current / changes / pending 与独立脚本一致，三个视图可输出 XLSX |

HTTP 验证使用本地真实服务。浏览器导航使用模拟 CDP 客户端；目标选择、HTTP 401 登录状态与验证状态由检测点提供 reason。Windows 窗口身份、焦点与键盘行为沿用相应模块的隔离测试。

## 15. 身份与兼容约定

解析规则身份由分段、字段及会影响解析输出的去重键计算；来源与分页配置冻结在运行记录，
业务排序、主键类型和可变字段形成台账策略。仅修改来源或排序策略时，解析 rule_id 保持一致。

已有规则快照继续按原 ID 读取。新布局从选定的不可变结果版本构造 Dataset 兼容视图，
旧 pages/state 数据集保留原读取路径；采用操作明确指定源目录和目标目录。

每次独立观察保留原文与来源。历史按捕获身份累积，重解析只替换该捕获在候选版本中的
解析引用。处理未完成、质量偏离和业务冲突分别记录；导出固定版本、策略、决定集合及基线。

## 16. v0.3.0 发布检查（2026-10-29）

- 版本号、v0.3.0 标签规则与发布说明对应；正常标签、错误标签和手动触发检查通过。
- 文档中的 12 个子命令检查通过。
- 18 项操作恢复回归通过；进程间锁测试显式从源码运行子进程，并禁用 site 包，支持干净检出环境。
- wheel 与源码包构建成功，包含四个业务模块、恢复测试、订单场景和发布说明。
- 在新建虚拟环境中安装 wheel，版本输出 0.3.0，内置 notice-list 样本回归通过。
