# 公告列表

从重复的公告容器提取编号、标题、日期、正文和链接，按日期维护当前列表。
第二次观察更新 N001，新增 N003；N002 保留上次已知内容。

文件命令在源码根目录执行；安装包用户进入 Python 环境的 `share/clip2table` 目录后执行相同命令。

```powershell
clip2table test scene:notice-list
clip2table task check scenes/notice-list/notices.toml --html-file scenes/notice-list/input/page-1.html --field title
clip2table task run -p scenes/notice-list/notices.toml --html-file scenes/notice-list/input/page-1.html -d out/notices --yes
clip2table task run -p scenes/notice-list/notices.toml --html-file scenes/notice-list/input/page-2.html -d out/notices --yes
clip2table ledger -d out/notices --view current -f xlsx -o out/notices.xlsx
```

`output/expected-current.csv` 是人工整理的独立答案。`scene.toml` 记录包版本、兼容要求、维护者、任务卡和样本顺序。
安装包包含场景文件，`clip2table test scene:notice-list` 可从安装环境执行样本回归。

接入自己的来源时，修改卡片的 region/records 和字段 sel/attr，运行 task check 检查值与来源位置。
区域候选可用 `--pick N` 预览、`--pick N --write` 保存。已有数据用 replay 应用新规则，再选择候选版本。
