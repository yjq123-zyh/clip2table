"""Validate the release workflow: YAML parses, and the version gate actually
runs and produces the outputs the later jobs consume.

`.github/workflows/release.yml` 是唯一一份「写错了不会在本地红、只会在推标签
那一刻红」的文件。而这个项目反复在说同一句话：一句没人验证的承诺会烂掉。
所以这里把它真跑一遍——不是读一遍看着对，是抠出那段脚本、给它假的事件上下文、
跑起来看退出码和 GITHUB_OUTPUT。

用法（需要 pyyaml；产品本身零依赖，这个依赖只属于这个检查脚本）：

    python -m pip install pyyaml
    python _dev/check_release_workflow.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WF = ROOT / ".github" / "workflows" / "release.yml"


def main() -> int:
    # Windows 的 CI runner 用 cp1252 当控制台编码，中文一 print 就
    # UnicodeEncodeError——那是「这台机器读不了中文」，不是「检查没通过」。
    # 两者的退出码都是 1，所以在 CI 上根本分不清。这里先把输出强制成 UTF-8。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    doc = yaml.safe_load(WF.read_text(encoding="utf-8"))
    print("YAML 解析通过，顶层键:", list(doc))

    # PyYAML 按 YAML 1.1 把裸 `on` 当成布尔 True，所以这里两个键都认。
    triggers = doc.get("on", doc.get(True))
    if not isinstance(triggers, dict):
        raise SystemExit(f"on: 段解析异常：{triggers!r}")
    if "workflow_dispatch" not in triggers:
        raise SystemExit("缺 workflow_dispatch：失败后没法手动补发")
    push = triggers.get("push") or {}
    if push.get("branches"):
        raise SystemExit(
            "release 还在监听 main 的分支推送：那意味着改一行 workflow 就发一次版"
        )
    tags = push.get("tags") or []
    if tags != ["v*"]:
        raise SystemExit(f"push.tags 是 {tags!r}，期望只有 ['v*']")
    if push.get("paths"):
        raise SystemExit("release 还挂着 paths 过滤：那正是「改 workflow 就发版」的来源")
    print("触发条件:", {"workflow_dispatch": True, "push.tags": tags})

    # tests.yml 也顺手校验一遍——它跑得比 release 勤，写坏了更费时间。
    tests_wf = ROOT / ".github" / "workflows" / "tests.yml"
    tests_doc = yaml.safe_load(tests_wf.read_text(encoding="utf-8"))
    print("tests.yml 解析通过，jobs:", list(tests_doc["jobs"]))
    if "workflow" not in tests_doc["jobs"]:
        raise SystemExit("tests.yml 里没有 workflow job：release.yml 就没人检查了")

    jobs = doc["jobs"]
    print("jobs:", list(jobs))

    # 1. job 依赖图必须无环，且发布 job 都在 build 之后
    for name, job in jobs.items():
        needs = job.get("needs") or []
        if isinstance(needs, str):
            needs = [needs]
        missing = [n for n in needs if n not in jobs]
        if missing:
            raise SystemExit(f"{name} 依赖了不存在的 job: {missing}")
        print(f"  {name}: needs={needs}")

    # 1b. 两个发布目标是并列的，不能互相依赖：
    #     PyPI 那步会先红（trusted publisher 还没配），它红了不能拦住 GitHub Release。
    rel_needs = jobs["github-release"].get("needs") or []
    if isinstance(rel_needs, str):
        rel_needs = [rel_needs]
    if "publish-pypi" in rel_needs:
        raise SystemExit("github-release 依赖了 publish-pypi：PyPI 一红会把 Release 一起拖死")
    pypi_needs = jobs["publish-pypi"].get("needs") or []
    if isinstance(pypi_needs, str):
        pypi_needs = [pypi_needs]
    if "build" not in pypi_needs:
        raise SystemExit("publish-pypi 没等 build 就跑：会发布没测过的产物")

    # 2. build 必须把 tag 传给下游，否则 github-release 拿不到版本号
    tag_out = (jobs["build"].get("outputs") or {}).get("tag")
    if not tag_out:
        raise SystemExit("build.outputs.tag 缺失：github-release 会拿不到版本号")
    print("build.outputs.tag =", tag_out)

    # 2b. 交给 PyPI 的 artifact 只能有 whl 与 tar.gz。
    #     pypa 那个 action 会把 packages-dir 里的陌生文件当错误，
    #     之前这里还打算往 dist/ 里塞 SHA256SUMS.txt，装好就炸。
    upload = next(s for s in jobs["build"]["steps"] if "upload-artifact" in str(s.get("uses", "")))
    patterns = [line.strip() for line in str(upload["with"]["path"]).splitlines() if line.strip()]
    print("artifact 包含:", patterns)
    for p in patterns:
        if not (p.endswith(".whl") or p.endswith(".tar.gz")):
            raise SystemExit(f"artifact 里混进了非分发包 {p}：PyPI 上传会因此失败")

    # 3. PyPI job 必须同时具备 id-token: write 与 pypi 环境
    pypi = jobs["publish-pypi"]
    perms = pypi.get("permissions") or {}
    if perms.get("id-token") != "write":
        raise SystemExit("publish-pypi 缺 id-token: write，OIDC 换不到令牌")
    env_name = (pypi.get("environment") or {}).get("name")
    if env_name != "pypi":
        raise SystemExit(f"environment 名是 {env_name!r}，与文档里的 pypi 不一致")
    print("publish-pypi: id-token=write, environment=", env_name)

    # 4. 把版本门那段脚本抠出来真跑一遍（给它假的事件上下文）
    step = next(s for s in jobs["build"]["steps"] if s.get("id") == "version")
    script = step["run"]

    pwsh = shutil.which("pwsh")
    if not pwsh:
        # 不是失败，是「这台机器验不了」。说清楚，别默默给个绿灯。
        print("\n⚠ 没有 pwsh（PowerShell 7），跳过脚本实跑，只做了结构检查")
        return 0

    with tempfile.TemporaryDirectory() as td:
        out_file = Path(td) / "github_output"
        ps1 = Path(td) / "gate.ps1"
        # GitHub Actions 把 run: 正文原样落成文件执行；UTF-8 无 BOM，与这里一致。
        ps1.write_text(script, encoding="utf-8")

        def run(event: str, ref: str) -> subprocess.CompletedProcess:
            out_file.write_text("", encoding="utf-8")
            env = {
                **os.environ,
                "GITHUB_OUTPUT": str(out_file),
                "EVENT_NAME": event,
                "REF_NAME": ref,
            }
            return subprocess.run(
                [pwsh, "-NoProfile", "-File", str(ps1)],
                cwd=ROOT,
                env=env,
                # 控制台是 GB2312，中文会变成替换字符；这里只关心退出码与
                # GITHUB_OUTPUT，所以宽松解码，不让编码问题伪装成脚本失败。
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )

        def outputs() -> dict[str, str]:
            return dict(
                line.split("=", 1)
                for line in out_file.read_text(encoding="utf-8").splitlines()
                if "=" in line
            )

        # 4a. 正常路径：标签与版本一致
        version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
        expected_tag = f"v{version}"
        r = run("push", expected_tag)
        print(f"\n[正常路径 push {expected_tag}] exit=", r.returncode)
        if r.returncode != 0:
            print("stdout:", (r.stdout or "").strip()[:800])
            print("stderr:", (r.stderr or "").strip()[:800])
            raise SystemExit("版本门在正常路径下失败了")
        got = outputs()
        print("GITHUB_OUTPUT:", got)
        if got.get("tag") != expected_tag or got.get("version") != version:
            raise SystemExit(f"tag 输出不对: {got}")

        # 4b. 反向验证：标签与版本不一致时必须失败
        mismatched_tag = expected_tag + "-mismatch"
        r2 = run("push", mismatched_tag)
        print(f"[反向 push {mismatched_tag}] exit=", r2.returncode)
        if r2.returncode == 0:
            raise SystemExit("标签与版本不一致却通过了——这道门是假的")

        # 4c. workflow_dispatch 不校验标签（手动补发是允许的）
        r3 = run("workflow_dispatch", "")
        print("[手动触发 workflow_dispatch] exit=", r3.returncode)
        if r3.returncode != 0:
            print("stdout:", (r3.stdout or "").strip()[:800])
            raise SystemExit("手动触发路径失败了")

    print("\n全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
