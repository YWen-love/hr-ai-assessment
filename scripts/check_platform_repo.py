"""部署前核对：魔搭创空间仓库里的文件，和本地待上传的内容是否一致。

为什么需要这个脚本
------------------
魔搭在网页「上传文件」或 git push 之后，会对仓库文件做内容合规扫描。
命中敏感规则的文件会被平台**自动回滚** —— 平台用 Administrator 账号生成一个提交：

    File xxx has been reverted due to sensitive file content or message

被回滚的文件不会进镜像。容器启动时 import 链在这个文件上断掉，
表现是「部署失败」+ ModuleNotFoundError，部署日志里只有一段 import 栈，
看不出是平台把文件删了。

本脚本只读地拉一份平台仓库（浅克隆，不落工作区），回答三个问题：
  1. 平台 master 少了哪些文件？   → 少的就是不会进镜像的
  2. 两边同名文件内容是否一致？   → 不一致说明平台上是旧版
  3. 平台最近的提交有没有「被回滚」记录？

比对对象默认是**本地工作区**（= `git add -A` 会纳入的内容 = 网页上传实际会带上去的
东西），因为部署失败排查看的是"即将上传的这份"，不是上一次提交。
要跟某次提交比，用 `--source HEAD`（或 `--source <sha>`）。

用法
----
    python scripts/check_platform_repo.py
    python scripts/check_platform_repo.py --source HEAD
    python scripts/check_platform_repo.py --remote https://www.modelscope.cn/studios/OWNER/NAME

退出码
------
    0  一致，可以部署
    1  有缺失 / 刚被回滚 —— 先解决再部署
    2  平台仓库读不到（网络/权限问题）
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

DEFAULT_REMOTE = "https://www.modelscope.cn/studios/YWen666/hr-ai-assessment"
REVERT_MARK = "has been reverted"


def _git(args: list[str], cwd: str | Path | None = None) -> tuple[int, str]:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _git_bytes(args: list[str], cwd: str | Path | None = None) -> bytes:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
    )
    return proc.stdout


def _tree(repo: str | Path) -> dict[str, str]:
    """HEAD 树：路径 -> blob sha。"""
    code, out = _git(["ls-tree", "-r", "HEAD"], cwd=repo)
    if code != 0:
        raise RuntimeError(out.strip() or "git ls-tree 失败")
    tree: dict[str, str] = {}
    for line in out.splitlines():
        meta, _, path = line.partition("\t")
        parts = meta.split()
        if len(parts) == 3 and parts[1] == "blob":
            tree[path.strip()] = parts[2]
    return tree


def _subjects(repo: str | Path, limit: int = 10) -> list[tuple[str, str, str]]:
    code, out = _git(
        ["log", f"-n{limit}", "--format=%H%x1f%ad%x1f%s", "--date=iso"], cwd=repo
    )
    if code != 0:
        return []
    rows = []
    for line in out.splitlines():
        parts = line.split("\x1f")
        if len(parts) == 3:
            rows.append((parts[0][:8], parts[1], parts[2]))
    return rows


def _normalize(data: bytes) -> bytes:
    """行尾规整：CRLF 与 LF 视为同一内容。

    平台上传会把部分文件的行尾规整成 CRLF，本地是 LF —— blob sha 因此不同，
    但内容其实一模一样。行尾差异不构成"平台上是旧版"，不该报成漂移。
    """
    return data.replace(b"\r\n", b"\n")


def _local_files(repo: str | Path, source: str) -> dict[str, bytes]:
    """本地待上传内容：路径 -> 字节。

    source="worktree"：工作区文件（已跟踪 + 未跟踪且未被 .gitignore 忽略），
                       相当于 `git add -A` 之后的样子。
    source 其他值：     该 commit（HEAD 或任意 sha）里的文件。
    """
    if source != "worktree":
        code, out = _git(["ls-tree", "-r", source], cwd=repo)
        if code != 0:
            raise RuntimeError(out.strip() or f"读 {source} 失败")
        files: dict[str, bytes] = {}
        for line in out.splitlines():
            meta, _, path = line.partition("\t")
            parts = meta.split()
            if len(parts) == 3 and parts[1] == "blob":
                files[path.strip()] = _git_bytes(["cat-file", "-p", parts[2]], cwd=repo)
        return files

    paths: list[str] = []
    for args in (["ls-files", "-z"], ["ls-files", "-z", "--others", "--exclude-standard"]):
        code, out = _git(args, cwd=repo)
        if code != 0:
            raise RuntimeError(out.strip() or "git ls-files 失败")
        paths.extend(p for p in out.split("\0") if p)

    files = {}
    for path in paths:
        full = Path(repo) / path
        if full.is_file():
            files[path] = full.read_bytes()
    return files


def main() -> int:
    parser = argparse.ArgumentParser(description="核对魔搭创空间仓库与本地的文件一致性")
    parser.add_argument("--remote", default=DEFAULT_REMOTE, help="魔搭创空间仓库地址")
    parser.add_argument("--ref", default="master", help="平台分支（默认 master）")
    parser.add_argument("--source", default="worktree", help="本地来源：worktree（默认）或某个 commit")
    parser.add_argument("--repo", default=str(Path(__file__).resolve().parents[1]), help="本地仓库")
    args = parser.parse_args()

    try:
        local = _local_files(args.repo, args.source)
    except RuntimeError as exc:
        print(f"读本地内容失败：{exc}")
        return 2
    if not local:
        print("本地没有可比对的文件")
        return 2

    with tempfile.TemporaryDirectory(prefix="ms-repo-check-") as tmp:
        clone = str(Path(tmp) / "repo")
        code, out = _git(
            [
                "clone",
                "--depth", "1",
                "--no-checkout",
                "--single-branch",
                "--branch", args.ref,
                args.remote,
                clone,
            ]
        )
        if code != 0:
            print(f"拉取平台仓库失败：\n{out.strip()}")
            return 2

        try:
            platform = _tree(clone)
        except RuntimeError as exc:
            print(f"读平台仓库失败：{exc}")
            return 2
        commits = _subjects(clone, limit=10)

        missing = sorted(set(local) - set(platform))
        extra = sorted(set(platform) - set(local))
        drifted: list[str] = []
        eol_only: list[str] = []
        for path in sorted(set(local) & set(platform)):
            if local[path] == _git_bytes(["cat-file", "-p", platform[path]], cwd=clone):
                continue
            if _normalize(local[path]) == _normalize(
                _git_bytes(["cat-file", "-p", platform[path]], cwd=clone)
            ):
                eol_only.append(path)
            else:
                drifted.append(path)

    source_label = "工作区" if args.source == "worktree" else args.source
    print(f"平台仓库：{args.remote} @{args.ref}")
    if commits:
        head = commits[0]
        print(f"平台 HEAD：{head[0]}  {head[1]}  {head[2]}")
    print(f"本地（{source_label}）文件 {len(local)} 个 / 平台文件 {len(platform)} 个")
    print("-" * 70)

    reverts = [c for c in commits if REVERT_MARK in c[2]]

    if reverts:
        print("平台最近提交里有回滚记录（这些文件没进镜像）：")
        for sha, when, subject in reverts:
            print(f"  ! {sha} {when}  {subject}")
        print()

    if missing:
        print(f"平台缺少 {len(missing)} 个文件（不会进镜像）：")
        for path in missing:
            print(f"  - {path}")
        print()

    if drifted:
        print(f"{len(drifted)} 个同名文件内容不一致（平台上是旧版）：")
        for path in drifted:
            print(f"  ~ {path}")
        print()

    if eol_only:
        print(f"{len(eol_only)} 个文件仅行尾符不同（CRLF/LF），内容一致，不影响部署")
        print()

    if extra:
        print(f"平台多出 {len(extra)} 个文件（本地没有，不影响启动）：")
        for path in extra:
            print(f"  + {path}")
        print()

    if missing or drifted or reverts:
        print("结论：不一致 —— 先把上面的文件同步到平台，确认没被回滚，再重新部署。")
        return 1

    print("结论：一致 —— 平台 master 与本地待上传内容的文件集和内容都相同，可以部署。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
