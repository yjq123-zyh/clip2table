# 发布

发一个版本 = 做三件事：改版本号、写发版说明、推一个标签。剩下的事 CI 干。

## CI 干了什么

`.github/workflows/release.yml` 在推 `v*` 标签或手动触发时运行，三个 job：

| job | 跑在哪 | 干什么 | 失败意味着 |
|---|---|---|---|
| `build` | windows | 版本号三方对齐（标签 / `pyproject.toml` / `__init__.py`）、跑全量单测、构建 wheel 与 sdist | 一个不一致或一条测试红，后面两个 job 都不会跑，任何东西都不会发出去 |
| `publish-pypi` | ubuntu | 用 OIDC 把 wheel 与 sdist 推到 PyPI | 需要 PyPI 侧的 pending publisher 已配好（见下） |
| `github-release` | windows | 建或更新 GitHub Release，附 wheel、sdist、`SHA256SUMS.txt` | 只有 `gh` 权限或标签冲突问题 |

`publish-pypi` 与 `github-release` 都只依赖 `build`，彼此不依赖——两个发布目标互不牵连。
PyPI 那边因为还没配好 trusted publisher 而红，不该连带让 GitHub Release 也发不出来；
反过来 GitHub Release 建失败时，直接「重跑失败的 job」即可，不会再去碰 PyPI
（那个版本已经传上去，第二次必然报 `File already exists`）。

顺序不是随意的：**先测、再构建、然后才允许有东西出门**。标签可能打在任何一个提交上，
而 `tests.yml` 只跑 `main` 的推送，所以 `build` 自己再跑一遍单测，不让「没测过的提交」
变成已发布的版本。

`github-release` 是幂等的：标签已经存在时改成覆盖资产，不重复建 Release。
`publish-pypi` 故意是**不**幂等的（没有 `skip-existing`）——同一版本传两次在 PyPI 上是错误，
让它红，红就是「你忘了升版本号」。

## 第一次发布：配置 Trusted Publishing

这一步只需要做一次，而且只能在网页上做（需要你的 PyPI 账号）。别用 API token：
token 是长期有效的密钥，要存、要轮换、泄露了能改包；OIDC 换来的是 15 分钟就失效、
只对这一次运行、只对这一个仓库有效的东西。

1. 打开 <https://pypi.org/manage/account/publishing/>
2. 填 **Pending publisher**（项目还没上传过时用这个），五个字段必须与 workflow 逐字一致：

   | 字段 | 值 |
   |---|---|
   | PyPI Project Name | `clip2table` |
   | Owner | `yjq123-zyh` |
   | Repository name | `clip2table` |
   | Workflow name | `release.yml` |
   | Environment name | `pypi` |

3. 保存。之后第一次成功上传会自动把它从 pending 转成正式 publisher。

任何一个字段改名，workflow 里都必须同步改，否则报 `invalid-publisher`（403）。
`release.yml` 里那段注释就是这个提醒。

## 发一个新版本

```bash
# 1. 三处版本号一起改
#    pyproject.toml 的 version
#    src/clip2table/__init__.py 的 __version__
#    CHANGELOG.md 加一节

# 2. 写发版说明（release.yml 找不到这个文件会直接失败）
$EDITOR docs/releases/v0.3.0.md

# 3. 提交，打标签，推
git add src tests scenes docs README.md SKILL.md CHANGELOG.md pyproject.toml
git commit -m "Release v0.3.0"
git tag v0.3.0
git push --atomic origin main v0.3.0
```

提交应包含新增模块、测试和场景文件。推送前核对暂存清单与安装包内容；本地计划和运行产物保持在发布清单之外。

推标签触发 workflow。也可以去 **Actions → release → Run workflow** 手动触发一次
（读的是当前分支的代码，不校验标签，适合修复一次失败的发布）。

## 出问题怎么看

`python _dev/check_release_workflow.py` 检查工作流结构，并使用 `pyproject.toml` 的当前版本
验证匹配标签、错误标签和手动触发三条路径。运行需要 PyYAML 和 PowerShell 7。

| 报错 | 原因 |
|---|---|
| `invalid-publisher` / 403 | PyPI 上的字段与 `release.yml` 不一致；或 environment 名字不是 `pypi` |
| `File already exists` | 这个版本号已经在 PyPI 上了。**不能覆盖**，只能升版本号 |
| `缺少发版说明 docs/releases/vX.md` | 忘了写发版说明 |
| `标签和版本号对不上` | 标签是 `v0.1.1`，但 `pyproject.toml` 里还是 `0.1.0` |
| `两个版本号必须一致` | `pyproject.toml` 与 `__init__.py` 不一致 |

已发布的版本不能撤回再重传：PyPI 允许 yank（把某个版本标记为不推荐），
但同一个版本号的文件一旦上传就不再接受新文件。所以升版本号是唯一的出路。
