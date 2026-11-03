# 版本兼容与采集后端

适用版本：0.4.0。

## 数据与接口

| 对象 | 读取 | 新写入 | 升级方式 |
|---|---|---|---|
| Python API | API 1 | API 1 | 原请求和四种操作状态保持；新增可选 JSON 输入、候选选择与 summary 导出 |
| 任务卡 | schema 1、2 | 文本沿用原格式，JSON/DOM 使用 2 | 保存后重载同一规则 |
| 解析结果 | 格式 1、2 | 格式 2 | 历史解析身份保留，新规则生成新解析 |
| 数据目录 | pages/state、布局 2、3 | 新数据集布局 3 | 采用到独立目标目录 |
| SQLite 索引 | 格式 1 | 格式 1 | 删除后由捕获、解析、修订和事件重建 |
| 场景清单 | schema 1 | schema 1 | 最低程序版本、卡片格式和字段能力由清单声明 |

布局 2 的整体采用：

```powershell
clip2table rev adopt --source out/old -d out/adopted
```

源目录保持原样，目标保存捕获、解析、修订、当前选择、人工决定、请求回执和运行检查点。
迁移中的导出保存暂存产物，恢复时继续发布到原指定输出路径。相同 request_id 可返回原结果；恢复操作沿用原 run_id。
pages/state 旧目录按页采用时继续使用 `--page`、`--kind` 等原参数。

`ledger_view()` 保留完整行列表返回形式。`query_view(..., limit=100, offset=0)` 返回一页行、字段和统计。
`ExportRequest.detail` 默认为 `full`，返回完整格式损失说明；`summary` 返回数量与伴随文件引用，CLI 导出使用 summary。

## 后端能力

| 能力 | HTTP | CDP 浏览器 | Win32 桌面 |
|---|---|---|---|
| 输入 | HTML、文本、JSON 响应 | 页面文本、HTML 快照 | 选区文本、剪贴板 HTML |
| 目标 | 请求 URL | 标签 ID、地址或标题 | 窗口句柄、进程和标题 |
| 就绪 | 响应完成 | 页面状态及目标区域 | 窗口激活与复制完成 |
| 分页 | URL 页码模板 | URL 模板、点击或按键 | 只读按键 |
| 恢复 | 页码模板定位 | URL 模板定位；点击路径确认位置 | 确认来源位置 |
| 结构定位 | JSON pointer、HTML 选择器 | HTML 选择器及快照节点 | HTML 快照节点或文本行 |
| 环境 | Python 3.11+ | Chromium 调试端口 | Windows 10/11 |

订单场景由 HTTP/JSON、HTML 快照和文本输入覆盖；公告场景使用 DOM 重复容器。
现有 urllib、CDP 和 Win32 实现完成这两类任务，运行时依赖保持标准库。
浏览器生命周期、导航和释放由统一会话管理；桌面操作完成后归还原窗口焦点。

实际验收环境为 Windows 11、Python 3.12.6、Microsoft Edge：本地浏览器两页采集得到三个实体，Win32 复制得到两行记录并恢复原剪贴板。
复现入口为 `_dev/check_sessions.py`，资源测量入口为 `_dev/benchmark_storage.py`。
