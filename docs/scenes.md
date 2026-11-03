# 场景包

场景包将任务卡、输入序列、使用步骤和独立答案放在同一目录。0.4.0 安装包提供订单台账与公告列表。

```powershell
clip2table test scene:order-ledger
clip2table test scene:notice-list
```

源码中的文件位于 `scenes/<名称>`；wheel 安装后位于 Python 环境的 `share/clip2table/scenes/<名称>`。
`clip2table test scene:<名称>` 自动定位两种目录。

## 清单

`scene.toml` 使用 schema 1：

```toml
schema = 1
id = "notice-list"
version = "1.0.0"
maintainer = "yjq123-zyh"
min_clip2table = "0.4.0"
card_schema = [2]
capabilities = ["dom"]
cards = ["notices.toml"]

[[samples]]
name = "current"
card = "notices.toml"
kind = "html"
inputs = ["input/page-1.html", "input/page-2.html"]
view = "current"
expected = "output/expected-current.csv"
```

文件引用相对场景目录。验证器检查程序版本、声明能力、卡片格式和文件引用，按输入顺序执行，再逐字段比较答案及顺序。
订单包同时提供文本、JSON、HTML 三组等价输入；公告包提供两次 DOM 观察。

## 维护

README 写明来源条件、字段定义、首次执行、重复运行、纠错重算和导出步骤。
输入使用脱敏样本；答案由人工或独立业务脚本生成。修改规则时同时运行场景回归。

字段或身份语义变化提升主版本；兼容能力增加提升次版本；样本与规则修正提升修订号。
页面结构变化后先通过 task check 查看字段位置和候选，保存选择，再用 replay 重算已有捕获并选择候选版本。
