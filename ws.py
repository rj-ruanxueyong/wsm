#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ws.py - 工作空间管理编排 CLI。

命令：
    ws.py init   --url <prj_compile_output_url> [--repos-root <path>] [--user <user_name>]
    ws.py add    --repo <仓库名> [--repo <仓库名> ...] [--no-fast]
    ws.py add    --img  [--url <url>] [--path <dir>]
    ws.py add    --header --url <url>
    ws.py remove --repo <仓库名> [--yes]
    ws.py remove --all [--yes]
    ws.py status | list [--repo|--component] [--all] | doctor [--net]

仓库根（REPOS_ROOT）与 git 用户名（USER_NAME，别名 REPOS_USER_NAME）已改为全局
配置 ~/.config/wsm/ws.config 管理，本地 .ws_env/ws.config 里的同名旧键仅作兜底；
其余配置（IMAGES_URL 等工作区属性）仍保存在工作区本地。

下载统一走 download.py，解析/解压统一走 extract.py。
"""

import argparse
import json
import os
import re
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wslib as ws  # noqa: E402

GERRIT_HOST = "gerrit.ruijie.work"
GERRIT_PORT = 29418
INTERNAL_GIT = "git@git.ruijie.net"
ISSUES_NAME = "issues.json"
IMAGES_NAME = "images.json"


# ---------------------------------------------------------------- 问题记录

def issues_path(root=None):
    return os.path.join(ws.log_dir(root), ISSUES_NAME)


def load_issues(root=None):
    return ws.load_json(issues_path(root), []) or []


def reset_issues(root=None):
    ws.save_json(issues_path(root), [])


def add_issue(step, target, code, message, root=None, level="ERROR"):
    """记录一条问题：写 log/ws.log（WARN/ERROR）+ log/issues.json（供 final.md 汇总）。"""
    ws.log("[%s] %s | %s | %s" % (step, target, code, message), level, root)
    data = load_issues(root)
    data.append({"time": ws.now_str(), "step": step, "target": target,
                 "code": code, "message": message, "level": level})
    ws.save_json(issues_path(root), data)


def run_step(step, target, func, *args, **kwargs):
    """执行一步，失败不抛出，记录问题并返回 (ok, value)。"""
    try:
        return True, func(*args, **kwargs)
    except ws.WsError as exc:
        add_issue(step, target, exc.code, exc.message)
        return False, None
    except Exception as exc:  # noqa: BLE001 - 兜底，保证批量流程不中断
        add_issue(step, target, "UNEXPECTED", "%s: %s" % (type(exc).__name__, exc))
        return False, None


# ---------------------------------------------------------------- images 记录

def images_path(root=None):
    return os.path.join(ws.env_dir(root), IMAGES_NAME)


def load_images(root=None):
    return ws.load_json(images_path(root), {}) or {}


def save_images(data, root=None):
    return ws.save_json(images_path(root), data)


def record_images(root, url, file_path, mode, dest, note=None):
    mtime = None
    if file_path and os.path.isfile(file_path):
        import time
        mtime = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(file_path)))
    size = os.path.getsize(file_path) if file_path and os.path.isfile(file_path) else None
    data = {"url": url, "file": file_path, "mode": mode, "dest": dest,
            "file_time": mtime, "size": size,
            "fetched_at": ws.now_str(), "note": note}
    save_images(data, root)
    return data


# ---------------------------------------------------------------- git 工具

def user_name_from_git():
    """从 git 全局配置的 user.email 推导用户名（邮箱 @ 前缀）。"""
    code, out, _ = ws.run(["git", "config", "--global", "user.email"])
    if code == 0 and out and "@" in out:
        return out.split("@")[0].strip()
    return None


def is_git_repo(path):
    return os.path.isdir(os.path.join(path, ".git"))


# git ref 名不允许的字符
_REF_BAD_CHARS = re.compile(r"[\s~^:?*\[\\]+|@\{")


def workspace_branch_name(root, repo_name):
    """本地分支名 = <工作区目录名>_<仓库名>。

    工作区目录名取工作区根的 basename（稳定，不受执行 cwd 影响），
    并按 git ref 规则做安全化处理；仓库名里的 '/' 换为 '-'。
    """
    base = os.path.basename(os.path.abspath(root).rstrip(os.sep)) or "ws"
    safe = _REF_BAD_CHARS.sub("-", base).replace("/", "-").strip(".-")
    safe = re.sub(r"\.{2,}", ".", safe)
    name = str(repo_name).replace("/", "-").strip(".-")
    return "%s_%s" % (safe or "ws", name or "repo")


def local_branch_exists(repo_path, branch_name):
    code, _, _ = ws.run(["git", "-C", repo_path, "show-ref", "--verify", "--quiet",
                         "refs/heads/%s" % branch_name])
    return code == 0


def branch_in_use(repo_path, branch_name):
    """判断本地分支是否已被某个 worktree 占用；返回占用路径或 None。"""
    code, out, _ = ws.run(["git", "-C", repo_path, "worktree", "list", "--porcelain"])
    if code != 0:
        return None
    current = None
    ref = "branch refs/heads/%s" % branch_name
    for line in out.splitlines():
        if line.startswith("worktree "):
            current = line[len("worktree "):].strip()
        elif line.strip() == ref:
            return current
    return None


def repo_source_urls(repo_name, user):
    """按优先级给出候选源：(source, url) 列表。"""
    sources = []
    if user:
        sources.append(("gerrit", "ssh://%s@%s:%d/%s" % (user, GERRIT_HOST,
                                                         GERRIT_PORT, repo_name)))
    sources.append(("internal-git", "%s:%s" % (INTERNAL_GIT, repo_name)))
    return sources


def install_commit_msg_hook(target, repo_name, user, root=None):
    """从 gerrit 取 commit-msg 钩子；失败只记 WARN，不影响主流程。"""
    if not user:
        return
    hook_dir = os.path.join(target, ".git", "hooks")
    code, _, err = ws.run(["scp", "-p", "-P", str(GERRIT_PORT),
                           "%s@%s:hooks/commit-msg" % (user, GERRIT_HOST), hook_dir])
    if code != 0:
        add_issue("clone", repo_name, "HOOK_INSTALL_FAILED",
                  "commit-msg 钩子安装失败: %s" % (err or "unknown"), root, "WARN")


def fast_fetch_repo(repo_root, repo_name, branch, user, root=None):
    """快速建立主克隆：git init + git remote add + git fetch --no-tags origin <分支号>。

    只拉取所需分支（无分支号时退化为拉取全部 heads），保留完整历史。
    fetch 会建立 `refs/remotes/origin/<分支号>`，因此后续
    `git worktree add -b <本地分支> origin/<分支号>` 与上游跟踪都不受影响。
    """
    target = os.path.join(repo_root, repo_name)
    if branch:
        refspec = "refs/heads/%s" % branch
    else:
        refspec = "+refs/heads/*:refs/remotes/origin/*"

    errors = []
    for source, url in repo_source_urls(repo_name, user):
        ws.log("正在从 %s 快速拉取 %s（%s）..." % (source, repo_name, branch or "全部 heads"),
               "INFO", root)
        started = time.time()
        try:
            shutil.rmtree(target, ignore_errors=True)
            ws.run(["git", "init", "-q", target], check=True)
            ws.run(["git", "-C", target, "remote", "add", "origin", url], check=True)
            code, out, err = ws.run(
                ["git", "-C", target, "fetch", "--no-tags", "--progress", "origin", refspec],
                echo=True)
            if code != 0:
                raise ws.WsError("FETCH_FAILED", err or out)
            if source == "gerrit":
                install_commit_msg_hook(target, repo_name, user, root)
            ws.log("快速拉取完成 %s（用时 %.1fs）" % (repo_name, time.time() - started),
                   "INFO", root)
            return target, source
        except ws.WsError as exc:
            errors.append("%s: %s" % (source, exc.message))
            shutil.rmtree(target, ignore_errors=True)

    raise ws.WsError("FAST_FETCH_FAILED",
                     "快速拉取 %s 失败（--fast）：\n%s" % (repo_name, "\n".join(errors)))


def obtain_repo(repo_root, repo_name, branch, user, root=None, fast=True):
    """取得仓库群里的主克隆，返回 (repo_path, source)。

    - `fast=True`（默认）：`git init` + `git fetch --no-tags origin <分支号>`，只取所需分支
    - `fast=False`（`--no-fast`）：整体 `git clone`，回退链 gerrit -> 内网 git
    - 仓库群里已有主克隆时，两种模式都直接复用
    """
    target = os.path.join(repo_root, repo_name)
    if is_git_repo(target):
        ws.log("仓库群已有 %s，直接复用" % repo_name, "INFO", root)
        return target, "existing"
    if fast:
        return fast_fetch_repo(repo_root, repo_name, branch, user, root)
    return clone_repo(repo_root, repo_name, user, root)


def clone_repo(repo_root, repo_name, user, root=None):
    """按 gerrit -> 内网 git 的顺序克隆仓库，返回 (repo_path, source)。

    克隆过程用 --progress + 实时转发，避免长命令看起来像卡死。
    """
    target = os.path.join(repo_root, repo_name)
    if is_git_repo(target):
        ws.log("仓库群已有 %s，直接复用" % repo_name, "INFO", root)
        return target, "existing"

    errors = []
    for source, url in repo_source_urls(repo_name, user):
        ws.log("正在从 %s 克隆 %s（%s）..." % (source, repo_name, url), "INFO", root)
        started = time.time()
        try:
            ws.run(["git", "clone", "--progress", url, target], check=True, echo=True)
            ws.log("克隆完成 %s（用时 %.1fs）" % (repo_name, time.time() - started),
                   "INFO", root)
            if source == "gerrit":
                install_commit_msg_hook(target, repo_name, user, root)
            return target, source
        except ws.WsError as exc:
            errors.append("%s: %s" % (source, exc.message))
            shutil.rmtree(target, ignore_errors=True)

    raise ws.WsError("CLONE_FAILED", "克隆 %s 失败：\n%s" % (repo_name, "\n".join(errors)))


def suggest_worktree_cmd(repo_path, target, branch, branch_name):
    """名称冲突时给出可直接复制的建议命令。

    同名分支不能被两个 worktree 检出，因此目录名与分支名一起递増（<名>_1）。
    """
    for idx in range(1, 100):
        cand = "%s_%d" % (branch_name, idx)
        cand_dir = "%s_%d" % (target, idx)
        if branch_in_use(repo_path, cand) or os.path.exists(cand_dir):
            continue
        if branch:
            return "git -C %s worktree add %s -b %s origin/%s" % (repo_path, cand_dir, cand, branch)
        return "git -C %s worktree add %s -b %s" % (repo_path, cand_dir, cand)
    return None


def worktree_add(repo_path, repo_name, branch, dest_root, branch_name, root=None):
    """在 dest_root 下为 repo_path 创建 worktree，返回记录。

    目录名固定为 <dest_root>/<仓库名>（与分支名解耦），
    本地分支名由调用方传入（通常为 <工作区目录名>_<仓库名>）。

    规则：
    - 目录已存在 / 本地分支已被其它 worktree 占用 -> 报错（附可复制的改名命令）
    - 本地分支存在但未被占用 -> 复用该分支
    - 本地分支不存在 -> 基于 origin/<分支号> 新建
    """
    target = os.path.join(dest_root, repo_name)
    if os.path.exists(target):
        raise ws.WsError("TARGET_EXISTS", "目标路径已存在: %s" % target)

    if branch:
        ws.log("正在拉取 %s@%s ..." % (repo_name, branch), "INFO", root)
        # 失败也不阻塞：后面 worktree add 会再报具体错误
        ws.run(["git", "-C", repo_path, "fetch", "--progress", "origin", branch],
               echo=True)

    owner = branch_in_use(repo_path, branch_name)
    if owner:
        hint = suggest_worktree_cmd(repo_path, target, branch, branch_name)
        if owner == target:
            msg = "该仓库已导入本工作区：%s" % owner
        else:
            msg = "本地分支 %s 已被其它 worktree 占用：%s" % (branch_name, owner)
        if hint:
            msg += "\n如需并存，请改名重建，例如：\n  %s" % hint
        raise ws.WsError("BRANCH_NAME_TAKEN", msg)

    reuse = local_branch_exists(repo_path, branch_name)
    if reuse:
        cmd = ["git", "-C", repo_path, "worktree", "add", target, branch_name]
    elif branch:
        cmd = ["git", "-C", repo_path, "worktree", "add", target, "-b", branch_name,
               "origin/%s" % branch]
    else:
        cmd = ["git", "-C", repo_path, "worktree", "add", target, "-b", branch_name]

    code, out, err = ws.run(cmd, echo=True)
    if code != 0:
        hint = suggest_worktree_cmd(repo_path, target, branch, branch_name)
        msg = "worktree 创建失败（%s）：%s" % (repo_name, err or out)
        if hint:
            msg += "\n分支/目录名冲突，可使用以下命令改名重建：\n  %s" % hint
        raise ws.WsError("WORKTREE_ADD_FAILED", msg)

    _, commit, _ = ws.run(["git", "-C", target, "rev-parse", "--short", "HEAD"])
    return {"repo_name": repo_name, "branch": branch, "branch_name": branch_name,
            "path": target, "repo_path": repo_path, "commit": commit,
            "mode": "worktree", "reused_branch": reuse,
            "created_at": ws.now_str()}


# ---------------------------------------------------------------- 仓库查询

def lookup_entry(repos, name):
    """按仓库名 / 兄弟仓库 / protobuf 工具仓库 / 归一化名 / 组件名查 repos.json。

    查找顺序：
      repos 精确名 -> rg_protobuf 兄弟仓库 -> rg-protobuf-* 工具仓库
      -> normalized -> components

    - 兄弟仓库（`rg_protobuf_r`）：在 repos.json 里只记容器 `rg-protobuf`，
      因此**分支号借用容器记录、仓库名保留传入值**。
    - 工具仓库（`rg-protobuf-tool`）：gerrit 上是**独立仓库**，仓库名保留自身，
      但 repos.json 里没有它的条目，**分支号从 `rg-protobuf` 记录取**
      （归一化会把 `rgprotobuftool` 当成另一回事，必须在这里先拦住）。
    """
    table = repos.get("repos", {})
    if name in table:
        return dict(table[name], matched_by="repo")

    container = table.get("rg-protobuf") or repos.get("normalized", {}).get("rgprotobuf")
    if ws.sibling_letter(name) and container:
        return {"repo_name": name, "branch": container.get("branch"),
                "commitid": container.get("commitid"), "version": container.get("version"),
                "repo_type": container.get("repo_type"),
                "repo_url": container.get("repo_url"),
                "pinned": container.get("pinned"),
                "components": [name], "matched_by": "sibling"}

    if ws.is_protobuf_tool(name) and container:
        return {"repo_name": name, "branch": container.get("branch"),
                "commitid": container.get("commitid"), "version": container.get("version"),
                "repo_type": container.get("repo_type"),
                "repo_url": container.get("repo_url"),
                "pinned": container.get("pinned"),
                "components": [name], "matched_by": "protobuf-tool"}

    entry = repos.get("normalized", {}).get(ws.norm_repo_key(name))
    if entry:
        return {"repo_name": entry["repo_names"][0], "branch": entry.get("branch"),
                "commitid": entry.get("commitid"), "version": entry.get("version"),
                "repo_type": entry.get("repo_type"), "repo_url": entry.get("repo_url"),
                "pinned": entry.get("pinned"), "components": entry["repo_names"],
                "matched_by": "normalized"}

    comp = repos.get("components", {}).get(name)
    if comp and comp.get("repo_name"):
        found = table.get(comp["repo_name"], {})
        return dict(found, repo_name=comp["repo_name"], branch=comp.get("branch"),
                    commitid=comp.get("commitid"), matched_by="component")
    return None


# ---------------------------------------------------------------- init

def cmd_init(args):
    root = os.path.abspath(os.getcwd())
    env = ws.ensure_dir(ws.env_dir(root))

    if not os.path.isdir(env):
        ws.emit_error("ENV_DIR_FAILED", "无法创建 %s" % env)

    gconf = ws.global_config_path()
    repos_root = args.repos_root or ws.config_value("REPOS_ROOT", root)
    if not repos_root:
        ws.emit_error("REPOS_ROOT_MISSING",
                      "缺少仓库群根路径，请用 --repos-root <path> 指定，"
                      "或在全局配置 %s 中设置 REPOS_ROOT" % gconf)
    repos_root = os.path.abspath(os.path.expanduser(repos_root))
    if not os.path.isdir(repos_root):
        ws.emit_error("REPOS_ROOT_INVALID", "仓库群根路径不存在: %s" % repos_root)

    user = args.user or ws.config_user_name(root) or user_name_from_git()
    if not user:
        ws.emit_error("USER_MISSING",
                      "缺少用户名，请用 --user <user_name> 指定，"
                      "或在全局配置 %s 设置 USER_NAME" % gconf)

    reset_issues(root)
    pub_dir = ws.ensure_dir(os.path.join(root, ws.PUB_DIR_NAME))
    repos_json = os.path.join(env, ws.REPOS_JSON_NAME)

    try:
        dl = ws.call_script("download.py", ["product", "--url", args.url, "--out", pub_dir], root)
        ex = ws.call_script("extract.py", ["product", "--dir", pub_dir, "--out", repos_json], root)
    except ws.WsError as exc:
        add_issue("init", args.url, exc.code, exc.message, root)
        render_final(root_int=root)
        ws.emit_error(exc.code, exc.message, issues=load_issues(root))

    product = ex.get("product", {})
    # REPOS_ROOT / 用户名已改为全局配置（~/.config/wsm/ws.config），不再写入本地
    ws.save_config({
        "COMPILE_OUTPUT_URL": args.url,
        "BUILD_PROJECT": product.get("build_project") or "",
        "PROJECT": product.get("project") or "",
        "IMAGES_URL": product.get("images") or "",
        "CREATED_AT": ws.now_str(),
    }, root)

    if not os.path.isfile(os.path.join(env, ws.WORKTREES_JSON_NAME)):
        ws.save_worktrees({"version": 1, "items": {}}, root)

    for item in ex.get("issues", {}).get("parse_errors", []):
        add_issue("extract", "rootfs.config", "COMPONENT_UNPARSED",
                  "组件行解析失败：%s" % item.get("raw"), root, "WARN")

    render_final(root_int=root)
    ws.emit({"ok": True, "action": "init", "root": root, "repos_root": repos_root,
             "user": user, "repos_json": repos_json,
             "product": product, "counts": ex.get("counts", {})})


# ---------------------------------------------------------------- add


def use_fast(args):
    """是否使用快速拉取：默认开启，`--no-fast` 关闭（`--fast` 保留为显式开启）。"""
    return not getattr(args, "no_fast", False)


def cmd_add(args):
    root = ws.find_ws_root()
    if args.header or args.img:
        if getattr(args, "fast", False) or getattr(args, "no_fast", False):
            ws.emit_error("ARG_CONFLICT", "--fast / --no-fast 只能与 --repo 一起使用")
        return add_image(root, args)
    if not args.repo:
        ws.emit_error("ARG_MISSING", "请指定 --repo <仓库名>，或使用 --img / --header")
    return add_repos(root, args)


def add_repos(root, args):
    repos = ws.load_repos(root)
    if not repos:
        ws.emit_error("REPOS_NOT_INITIALIZED",
                      "未找到 .ws_env/repos.json，请先执行 ws.py init --url <url>")

    worktrees = ws.load_worktrees(root)
    user = ws.config_user_name(root) or user_name_from_git() or ""
    repos_root = ws.config_value("REPOS_ROOT", root)
    if not repos_root:
        ws.emit_error("REPOS_ROOT_MISSING",
                      "缺少 REPOS_ROOT，请在全局配置 %s 中设置"
                      % ws.global_config_path())
    repos_root = os.path.abspath(os.path.expanduser(repos_root))

    results = []
    fast = use_fast(args)
    if not fast:
        ws.log("已关闭快速拉取（--no-fast），主克隆将用整体 git clone", "INFO", root)
    for name in args.repo:
        ok, value = run_step("add", name, add_one_repo, root, repos, worktrees,
                             repos_root, user, name, fast)
        if ok:
            value["ok"] = True
            results.append(value)
            worktrees["items"][value["repo_name"]] = value
            ws.save_worktrees(worktrees, root)
        else:
            results.append({"repo_name": name, "ok": False,
                            "message": load_issues(root)[-1]["message"]})

    render_final(root_int=root)
    failed = [r for r in results if not r.get("ok")]
    ws.emit({"ok": not failed, "action": "add", "added": results,
             "warnings": [i for i in load_issues(root) if i.get("level") == "WARN"],
             "issues": [i for i in load_issues(root) if i.get("level") == "ERROR"]})
    sys.exit(1 if failed else 0)


def add_one_repo(root, repos, worktrees, repos_root, user, name, fast=True):
    """添加单个仓库：查表 -> 取得主克隆 -> worktree。"""
    entry = lookup_entry(repos, name)
    if entry is None:
        add_issue("add", name, "REPO_NOT_IN_TABLE",
                  "repos.json 中未找到该名称，直接从原始仓库克隆到工作区", root, "WARN")
        if fast:
            ws.log("表外名称无分支号信息，快速拉取不适用，改为普通克隆：%s" % name,
                   "WARN", root)
        target = os.path.join(root, name)
        if os.path.exists(target):
            raise ws.WsError("TARGET_EXISTS", "目标路径已存在: %s" % target)
        errors = []
        source = None
        if user:
            url = "ssh://%s@%s:%d/%s" % (user, GERRIT_HOST, GERRIT_PORT, name)
            ws.log("正在从 gerrit 克隆 %s（%s）..." % (name, url), "INFO", root)
            code, _, err = ws.run(["git", "clone", "--progress", url, target], echo=True)
            if code == 0:
                source = "gerrit"
            else:
                errors.append("gerrit: %s" % err)
                shutil.rmtree(target, ignore_errors=True)
        if source is None:
            url = "%s:%s" % (INTERNAL_GIT, name)
            ws.log("正在从内网 %s 克隆 %s ..." % (INTERNAL_GIT, name), "INFO", root)
            code, _, err = ws.run(["git", "clone", "--progress", url, target], echo=True)
            if code == 0:
                source = "internal-git"
            else:
                errors.append("internal-git: %s" % err)
                shutil.rmtree(target, ignore_errors=True)
        if source is None:
            raise ws.WsError("CLONE_FAILED", "克隆 %s 失败：\n%s" % (name, "\n".join(errors)))
        _, commit, _ = ws.run(["git", "-C", target, "rev-parse", "--short", "HEAD"])
        # 表外克隆也按统一规则改分支名，让 `remove` 能识别并清理
        branch_name = workspace_branch_name(root, name)
        _, cur_branch, _ = ws.run(["git", "-C", target, "rev-parse", "--abbrev-ref", "HEAD"])
        if cur_branch != branch_name:
            code, _, err = ws.run(["git", "-C", target, "branch", "-m", branch_name])
            if code != 0:
                ws.log("重命名分支为 %s 失败（保留 %s）: %s" % (branch_name, cur_branch, err),
                       "INFO", root)
                branch_name = cur_branch
        return {"repo_name": name, "branch": None, "branch_name": branch_name,
                "path": target, "repo_path": target, "commit": commit,
                "mode": "clone", "source": source,
                "pinned": False, "created_at": ws.now_str()}

    repo_name = entry.get("repo_name") or name
    branch = entry.get("branch")
    repo_path, source = obtain_repo(repos_root, repo_name, branch, user, root, fast)

    branch_name = workspace_branch_name(root, repo_name)
    record = worktree_add(repo_path, repo_name, branch, root, branch_name, root)
    record["source"] = source
    record["commitid"] = entry.get("commitid")
    record["pinned"] = entry.get("pinned")
    record["matched_by"] = entry.get("matched_by")
    if entry.get("repo_type") == "svn":
        record["note"] = "svn 组件映射为 git 仓库，按分支 tip 检出，未按 svn version 固定"
        add_issue("add", repo_name, "SVN_UNPINNED", record["note"], root, "WARN")
    return record


def link_images(root, dest_dir, force=False):
    """把工作区 ./images 指向 dest_dir。"""
    link = os.path.join(root, ws.IMAGES_DIR_NAME)
    dest_dir = os.path.abspath(dest_dir)
    if os.path.islink(link):
        os.remove(link)
    elif os.path.isdir(link):
        if not force:
            raise ws.WsError("IMAGES_EXISTS",
                             "%s 已存在且不是软链接，请先清理或加 --force" % link)
        shutil.rmtree(link)
    elif os.path.exists(link):
        os.remove(link)
    os.symlink(dest_dir, link)
    return link


def add_image(root, args):
    """处理 add --img / add --header。"""
    repos = ws.load_repos(root)
    images_url = args.url or (repos.get("product", {}) or {}).get("images") \
        or ws.config_value("IMAGES_URL", root)
    if not images_url:
        ws.emit_error("IMAGES_URL_MISSING",
                      "未指定 --url，且 repos.json/ws.config 中无 images 地址，请先执行 init")

    pub_dir = ws.ensure_dir(os.path.join(root, ws.PUB_DIR_NAME))
    local_images = os.path.join(root, ws.IMAGES_DIR_NAME)

    # 已指定本地路径且其中已有 images 目录：直接软链，不重复下载
    if args.path:
        src = os.path.abspath(os.path.expanduser(args.path))
        if os.path.isdir(os.path.join(src, ws.IMAGES_DIR_NAME)):
            link = link_images(root, os.path.join(src, ws.IMAGES_DIR_NAME), args.force)
            record_images(root, images_url, None, "symlink",
                          os.path.join(src, ws.IMAGES_DIR_NAME), "使用已有 images 目录")
            render_final(root_int=root)
            ws.emit({"ok": True, "action": "img", "mode": "symlink", "link": link,
                     "source": os.path.join(src, ws.IMAGES_DIR_NAME)})
            return
        dest_dir = os.path.join(src, ws.IMAGES_DIR_NAME)
    else:
        dest_dir = local_images

    try:
        dl = ws.call_script("download.py", ["image", "--url", images_url, "--out", pub_dir], root)
        tar_path = dl["file"]["path"]
        ex_args = ["image", "--file", tar_path, "--dir", dest_dir]
        if args.header:
            ex_args.append("--header")
        ws.call_script("extract.py", ex_args, root)
    except ws.WsError as exc:
        add_issue("add-image", images_url, exc.code, exc.message, root)
        render_final(root_int=root)
        ws.emit_error(exc.code, exc.message, issues=load_issues(root))

    mode = "header" if args.header else "extract"
    note = "只保留 header 目录" if args.header else None
    if args.path:
        link = link_images(root, dest_dir, args.force)
    else:
        link = local_images
    record_images(root, images_url, tar_path, mode, dest_dir, note)
    render_final(root_int=root)
    ws.emit({"ok": True, "action": "img", "mode": mode, "link": link, "dest": dest_dir,
             "header": os.path.join(dest_dir, "header") if args.header else None,
             "file": tar_path})


# ---------------------------------------------------------------- 清理

def worktree_state(entry):
    """检查 worktree 的未提交/未推送情况。"""
    path = entry.get("path")
    state = {"exists": os.path.isdir(path or "")}
    if not state["exists"]:
        return state
    _, dirty, _ = ws.run(["git", "-C", path, "status", "--porcelain"])
    state["dirty"] = [l for l in dirty.splitlines() if l.strip()]
    code, ahead, _ = ws.run(["git", "-C", path, "log", "--oneline", "@{u}..HEAD"])
    if code != 0:
        state["no_upstream"] = True
        state["unpushed"] = []
    else:
        state["no_upstream"] = False
        state["unpushed"] = [l for l in ahead.splitlines() if l.strip()]
    return state


def inspect_and_confirm(entries, yes, action_desc, root=None):
    """删除前检查并确认：有风险时列出明细，需用户确认。"""
    risks = []
    for name, entry in entries:
        st = worktree_state(entry)
        if not st["exists"]:
            continue
        if st.get("dirty"):
            risks.append("%s：有 %d 项未提交改动（工作区/暂存）" % (name, len(st["dirty"])))
        if st.get("no_upstream"):
            risks.append("%s：分支无上游，无法确认是否有未推送修订" % name)
        elif st.get("unpushed"):
            risks.append("%s：有 %d 个未推送提交" % (name, len(st["unpushed"])))

    if risks:
        ws.log("%s 前发现风险：" % action_desc, "WARN", root)
        for line in risks:
            ws.log("  - %s" % line, "WARN", root)
    else:
        ws.log("%s 前检查通过：无未提交改动/未推送提交" % action_desc, "INFO", root)

    prompt = "确认%s（共 %d 个 worktree%s）？" % (
        action_desc, len(entries), "，存在未提交/未推送内容" if risks else "")
    if not ws.confirm(prompt, yes):
        ws.log("用户取消操作", "WARN", root)
        return False, risks
    return True, risks


def delete_local_branch(repo_path, branch_name, root=None):
    """删除 worktree 对应的本地分支；不适用的场景静默跳过。

    - 分支名为空 / 主克隆不存在 / 分支不存在 -> 跳过
    - 分支仍被其它 worktree 检出 -> 跳过并记 WARN（不能强删）
    - 未推送提交**不阻止**删除（确认环节已提示，用户确认即视为同意）
    """
    if not branch_name or not repo_path or not os.path.isdir(repo_path):
        return False
    if not local_branch_exists(repo_path, branch_name):
        return False
    owner = branch_in_use(repo_path, branch_name)
    if owner:
        add_issue("remove", branch_name, "BRANCH_IN_USE",
                  "本地分支 %s 仍被 worktree 占用（%s），未删除" % (branch_name, owner),
                  root, "WARN")
        return False
    code, _, err = ws.run(["git", "-C", repo_path, "branch", "-D", branch_name])
    if code != 0:
        add_issue("remove", branch_name, "BRANCH_DELETE_FAILED",
                  "删除本地分支 %s 失败: %s" % (branch_name, err or "unknown"), root, "WARN")
        return False
    ws.log("已删除本地分支 %s" % branch_name, "INFO", root)
    return True


def do_remove_worktree(entry, prune=False, root=None):
    """删除单个 worktree（连带其本地分支），可选 prune。"""
    path = entry.get("path")
    repo_path = entry.get("repo_path")
    if not path or not os.path.isdir(path):
        add_issue("remove", entry.get("repo_name", "?"), "WORKTREE_MISSING",
                  "worktree 目录不存在，已跳过: %s" % path, root, "WARN")
    else:
        code, out, err = ws.run(["git", "-C", repo_path, "worktree", "remove", path])
        if code != 0:
            code, out, err = ws.run(["git", "-C", repo_path, "worktree", "remove", "--force", path])
        if code != 0:
            raise ws.WsError("WORKTREE_REMOVE_FAILED",
                             "删除 worktree 失败 %s: %s" % (path, err or out))
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)

    # 仅 worktree 模式才删本地分支（clone 模式是普通克隆，整目录删掉即可）
    if entry.get("mode") == "worktree":
        delete_local_branch(repo_path, entry.get("branch_name"), root)

    if prune and repo_path and os.path.isdir(repo_path):
        ws.run(["git", "-C", repo_path, "worktree", "prune"])
    return True


def resolve_targets(root, worktrees, repo_name=None, all_flag=False):
    """确定要处理的目标 worktree 列表。"""
    items = worktrees.get("items", {})
    if all_flag:
        return [(k, v) for k, v in items.items() if os.path.isdir(v.get("path", ""))]
    if repo_name:
        entry = items.get(repo_name)
        if entry is None:
            for name, value in items.items():
                if os.path.basename(value.get("path", "")) == repo_name:
                    entry, repo_name = value, name
                    break
        if entry is None:
            ws.emit_error("WORKTREE_NOT_FOUND", "未记录该 worktree: %s" % repo_name)
        return [(repo_name, entry)]
    return []


def cmd_remove(args):
    """移除 worktree 并 prune。

    - `--repo <仓库名>`：移除指定 worktree
    - `--all`：移除全部已记录的 worktree
    两者删除后都会执行 `git worktree prune`。
    """
    root = ws.find_ws_root()
    if not args.all and not args.repo:
        ws.emit_error("ARG_MISSING", "请指定 --repo <仓库名> 或 --all")

    worktrees = ws.load_worktrees(root)
    targets = resolve_targets(root, worktrees, args.repo, args.all)
    if not targets:
        ws.emit_error("NOTHING_TO_REMOVE", "没有可移除的 worktree")

    desc = "移除全部 worktree（%d 个）" % len(targets) if args.all else "移除 worktree"
    ok, _ = inspect_and_confirm(targets, args.yes, desc, root)
    if not ok:
        ws.emit_error("USER_ABORTED", "用户取消操作")

    for name, entry in targets:
        good, _ = run_step("remove", name, do_remove_worktree, entry, True, root)
        if good:
            worktrees["items"].pop(name, None)
    ws.save_worktrees(worktrees, root)
    render_final(root_int=root)
    ws.emit({"ok": True, "action": "remove", "removed": [n for n, _ in targets],
             "warnings": [i for i in load_issues(root) if i.get("level") == "WARN"],
             "issues": [i for i in load_issues(root) if i.get("level") == "ERROR"]})


# ---------------------------------------------------------------- 辅助

def cmd_list(args):
    root = ws.find_ws_root()
    repos = ws.load_repos(root)
    worktrees = ws.load_worktrees(root)
    if args.component:
        ws.emit({"ok": True, "action": "list", "components": repos.get("components", {})})
        return
    if args.all:
        ws.emit({"ok": True, "action": "list", "repos": repos.get("repos", {}),
                 "normalized": repos.get("normalized", {})})
        return
    ws.emit({"ok": True, "action": "list", "worktrees": worktrees.get("items", {}),
             "images": load_images(root)})


def cmd_status(args):
    root = find_or_fail()
    render_final(root_int=root)
    ws.emit(collect_status(root))


def find_or_fail():
    root = ws.find_ws_root()
    if not ws.is_ws_root(root):
        ws.emit_error("NOT_INITIALIZED",
                      "当前目录未初始化工作空间（未找到含 ws.config 的 .ws_env）")
    return root


def collect_status(root):
    cfg = ws.load_config(root)
    repos = ws.load_repos(root)
    worktrees = ws.load_worktrees(root)
    items = []
    for name, entry in worktrees.get("items", {}).items():
        st = worktree_state(entry)
        items.append({
            "repo_name": name,
            "branch": entry.get("branch"),
            "branch_name": entry.get("branch_name"),
            "commit": entry.get("commit"),
            "path": entry.get("path"),
            "exists": st.get("exists", False),
            "dirty": len(st.get("dirty", [])),
            "unpushed": len(st.get("unpushed", [])),
            "no_upstream": st.get("no_upstream", False),
            "pinned": entry.get("pinned"),
            "note": entry.get("note"),
        })
    return {"ok": True, "action": "status", "root": root,
            "config": {"project": cfg.get("PROJECT"), "build_project": cfg.get("BUILD_PROJECT"),
                       "repos_root": cfg.get("REPOS_ROOT"),
                       "user": ws.config_user_name(root)},
            "images": load_images(root), "worktrees": items,
            "warnings": [i for i in load_issues(root) if i.get("level") == "WARN"],
            "issues": [i for i in load_issues(root) if i.get("level") == "ERROR"]}


def cmd_doctor(args):
    root = ws.find_ws_root()
    checks = []

    def check(name, ok, detail=""):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    check("git 可用", shutil.which("git") is not None, shutil.which("git") or "未找到 git")
    env = ws.env_dir(root)
    check(".ws_env 已初始化（含 ws.config）", ws.is_ws_root(root), env)
    for fname in (ws.CONFIG_NAME, ws.REPOS_JSON_NAME):
        path = os.path.join(env, fname)
        check("%s 可读取" % fname, os.path.isfile(path), path)

    gpath = ws.global_config_path()
    check("全局配置存在", os.path.isfile(gpath), gpath)
    gcfg = ws.load_global_config()
    guser = gcfg.get("USER_NAME") or gcfg.get(ws.USER_NAME_ALT_KEY)
    check("全局配置含用户名", bool(guser), guser or "未设置")

    cfg = ws.load_config(root)
    repos_root = cfg.get("REPOS_ROOT")
    check("REPOS_ROOT 已配置", bool(repos_root), repos_root or "未配置")
    check("REPOS_ROOT 存在", bool(repos_root) and os.path.isdir(repos_root), repos_root or "")

    repos = ws.load_repos(root)
    check("repos.json 含仓库索引", bool(repos.get("repos")), "repos=%d" % len(repos.get("repos", {})))

    if args.net:
        # gerrit 的 SSH 命令集里没有 shell 的 true，必须用 gerrit 自己的子命令
        guser = ws.config_user_name(root) or "git"
        code, out, err = ws.run(["ssh", "-p", str(GERRIT_PORT), "-o", "BatchMode=yes",
                                 "-o", "ConnectTimeout=5", "%s@%s" % (guser, GERRIT_HOST),
                                 "gerrit version"])
        check("gerrit 可达", code == 0, (out or err or "ok").strip())

    bad = [c for c in checks if not c["ok"]]
    ws.emit({"ok": not bad, "action": "doctor", "checks": checks})
    sys.exit(1 if bad else 0)


# ---------------------------------------------------------------- final.md

def render_final(root_int=None):
    """生成/覆盖 log/final.md 结果汇总。"""
    root = root_int or ws.find_ws_root()
    # 用宽松判定：init 失败时 ws.config 尚未写入，也需要生成汇总便于定位
    if not ws.has_env_dir(root):
        return None

    status = collect_status(root)
    cfg = status["config"]
    images = status["images"] or {}
    issues = status["issues"] + status["warnings"]
    wt = status["worktrees"]

    lines = []
    lines.append("# 工作空间创建结果汇总")
    lines.append("")
    lines.append("- 生成时间：%s" % ws.now_str())
    lines.append("- 工作空间：`%s`" % root)
    lines.append("- 编译工程：%s　项目：%s" % (cfg.get("build_project") or "-", cfg.get("project") or "-"))
    lines.append("- 仓库群根路径：`%s`" % (cfg.get("repos_root") or "-"))
    lines.append("- 用户名：%s" % (cfg.get("user") or "-"))
    lines.append("")

    lines.append("## 1. 概览")
    lines.append("")
    lines.append("| 项目 | 数量 |")
    lines.append("| --- | --- |")
    lines.append("| worktree | %d |" % len(wt))
    lines.append("| 其中异常（目录缺失） | %d |" % len([w for w in wt if not w["exists"]]))
    lines.append("| 问题记录 | %d |" % len(issues))
    errors = [i for i in issues if i.get("level") == "ERROR"]
    warns = [i for i in issues if i.get("level") == "WARN"]
    lines.append("| 其中错误 / 告警 | %d / %d |" % (len(errors), len(warns)))
    lines.append("")

    lines.append("## 2. worktree 状态")
    lines.append("")
    if wt:
        lines.append("| 仓库 | 分支 | 本地分支名 | 提交 | 目录 | 状态 |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for item in wt:
            if not item["exists"]:
                state = "目录缺失"
            else:
                flags = []
                if item["dirty"]:
                    flags.append("未提交 %d" % item["dirty"])
                if item["no_upstream"]:
                    flags.append("无上游")
                elif item["unpushed"]:
                    flags.append("未推送 %d" % item["unpushed"])
                if not item["pinned"]:
                    flags.append("未固定提交")
                state = "、".join(flags) if flags else "正常"
            lines.append("| %s | %s | %s | %s | `%s` | %s |" % (
                item["repo_name"], item["branch"] or "-", item["branch_name"] or "-",
                item["commit"] or "-", item["path"] or "-", state))
    else:
        lines.append("（暂无 worktree）")
    lines.append("")

    lines.append("## 3. images")
    lines.append("")
    if images:
        lines.append("- 来源：`%s`" % (images.get("url") or "-"))
        lines.append("- 本地镜像目录：`%s`（%s）" % (images.get("dest") or "-", images.get("mode") or "-"))
        lines.append("- 包版本时间（文件修改时间）：%s" % (images.get("file_time") or "-"))
        lines.append("- 获取时间：%s" % (images.get("fetched_at") or "-"))
        if images.get("note"):
            lines.append("- 备注：%s" % images["note"])
    else:
        lines.append("（暂未获取 images）")
    lines.append("")

    lines.append("## 4. 问题记录")
    lines.append("")
    if issues:
        lines.append("| 时间 | 步骤 | 对象 | 级别 | 代码 | 说明 |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for item in issues:
            lines.append("| %s | %s | %s | %s | %s | %s |" % (
                item.get("time", "-"), item.get("step", "-"), item.get("target", "-"),
                item.get("level", "-"), item.get("code", "-"),
                (item.get("message") or "").replace("\n", " ").replace("|", "\\|")))
    else:
        lines.append("（无）")
    lines.append("")

    lines.append("## 5. 备注")
    lines.append("")
    lines.append("- svn 型组件已映射为对应 git 仓库，按分支 tip 检出，无法按 svn version 固定提交。")
    lines.append("- 「未固定提交」表示当前记录未锁定 commitid，请以实际检出为准。")
    lines.append("- 本文件由 `ws.py` 覆盖生成，仅保留最新一次执行结果。")
    lines.append("")

    path = ws.final_path(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    return path


# ---------------------------------------------------------------- CLI

def build_parser():
    parser = argparse.ArgumentParser(prog="ws.py", description="工作空间管理编排 CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="初始化工作空间（下载并解析编译产物信息）")
    p.add_argument("--url", required=True, help="项目 compile_output 链接")
    p.add_argument("--repos-root", default=None,
                   help="仓库群根路径（缺省读全局配置 ~/.config/wsm/ws.config）")
    p.add_argument("--user", default=None,
                   help="用户名（缺省读全局配置，可临时覆盖）")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("add", help="导入仓库分支 / 获取 images")
    p.add_argument("--repo", action="append", default=None, help="仓库名（可重复）")
    fast_group = p.add_mutually_exclusive_group()
    fast_group.add_argument("--fast", action="store_true",
                            help="快速拉取（默认已开启，此为显式写法）：git init + fetch --no-tags origin <分支号>")
    fast_group.add_argument("--no-fast", action="store_true",
                            help="关闭快速拉取，改用整体 git clone（仓库很大时较慢，仅在不便按分支号拉取时使用）")
    p.add_argument("--img", action="store_true", help="获取 images")
    p.add_argument("--header", action="store_true", help="获取 images 且只保留 header 目录")
    p.add_argument("--url", default=None, help="images 包链接（缺省从 repos.json 取）")
    p.add_argument("--path", default=None, help="images 存放路径")
    p.add_argument("--force", action="store_true", help="覆盖已存在的 images 链接/目录")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("remove", help="移除 worktree 并 prune（--repo 指定单个，--all 全部）")
    p.add_argument("--repo", default=None, help="仓库名")
    p.add_argument("--all", action="store_true", help="移除全部已记录的 worktree")
    p.add_argument("--yes", action="store_true", help="跳过交互确认")
    p.set_defaults(func=cmd_remove)

    p = sub.add_parser("list", help="查看仓库 / 组件 / worktree")
    p.add_argument("--repo", action="store_true", help="按仓库名列出")
    p.add_argument("--component", action="store_true", help="按组件名列出")
    p.add_argument("--all", action="store_true", help="列出全部仓库索引")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("status", help="查看当前工作空间状态并刷新 log/final.md")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("doctor", help="自检环境与配置")
    p.add_argument("--net", action="store_true", help="额外检查 gerrit 连通性")
    p.set_defaults(func=cmd_doctor)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except ws.WsError as exc:
        ws.emit_error(exc.code, exc.message, **exc.extra)
    except KeyboardInterrupt:
        ws.emit_error("INTERRUPTED", "用户中断")
    return 0


if __name__ == "__main__":
    sys.exit(main())
