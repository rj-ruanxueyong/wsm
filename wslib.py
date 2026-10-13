#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wslib.py - 工作空间公共库（配置读写 / 日志 / 子进程调用 / 结果 JSON）。

被 ws.py、download.py、extract.py 共用。不单独作为命令使用。
"""

import json
import os
import re
import select
import subprocess
import sys
import time
from datetime import datetime

# ---------------------------------------------------------------- 路径与常量

ENV_DIR_NAME = ".ws_env"
CONFIG_NAME = "ws.config"
GLOBAL_CONFIG_SUBDIR = "wsm"           # 全局配置子目录（$XDG_CONFIG_HOME/wsm/）
USER_NAME_ALT_KEY = "REPOS_USER_NAME"  # 用户名在全局配置中的别名写法
# 仓库根 / git 用户名「改为全局」：全局配置优先，本地同名旧键仅作兜底
GLOBAL_FIRST_KEYS = ("REPOS_ROOT", "USER_NAME", USER_NAME_ALT_KEY)
REPOS_JSON_NAME = "repos.json"
WORKTREES_JSON_NAME = "worktrees.json"
LOG_DIR_NAME = "log"              # 日志目录（工作区根）
LOG_NAME = "ws.log"               # 异常/错误日志文件名
FINAL_NAME = "final.md"
PUB_DIR_NAME = ".ws_pub"          # 中间产物（下载的 product_info / rootfs.config / tar）
IMAGES_DIR_NAME = "images"        # 镜像目录（可为软链接）

# 需要写入日志文件的级别；INFO 只回显到 stderr 不落盘
LOG_LEVELS = ("WARN", "ERROR")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------- 输出协议

def emit(data):
    """把结果 JSON 写到 stdout，供调用方解析。"""
    sys.stdout.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    sys.stdout.flush()


def emit_error(code, message, **extra):
    """失败结果 JSON + 非 0 退出码，同时把该错误落盘到 log/。"""
    log("[%s] %s" % (code, message), "ERROR")
    data = {"ok": False, "code": code, "message": message}
    data.update(extra)
    emit(data)
    sys.exit(1)


# ---------------------------------------------------------------- 进度提示

def human_size(size):
    """字节数 -> 人类可读字符串。"""
    if size is None:
        return "未知"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return ("%d B" % value) if unit == "B" else ("%.1f %s" % (value, unit))
        value /= 1024.0
    return "%.1f TB" % value


def cmd_label(cmd, limit=120):
    """命令的可读标签，用于心跳等提示。"""
    text = " ".join(cmd) if isinstance(cmd, (list, tuple)) else str(cmd)
    return text if len(text) <= limit else text[:limit - 3] + "..."


class Progress(object):
    """把进度按节流间隔输出到 stderr 的小助手。

    - `total` 为 None 时只报告累计量
    - `unit="B"` 按 KB/MB/GB 显示；`unit=""` 显示原始计数（如解压成员数）
    - 只在**进度变化**且间隔达 `interval` 秒时输出，避免刷屏
    - stderr 不是终端时逐行输出（否则 `\r` 覆盖会污染日志/管道）
    - 始终写 stderr，绝不污染 stdout 的结果 JSON 协议
    """

    def __init__(self, label, total=None, unit="B", interval=1.0):
        self.label = label
        self.total = total if total else None
        self.unit = unit
        self.interval = interval
        self.count = 0
        self.started = time.time()
        self._last_emit = self.started

    def _fmt(self, value):
        return human_size(value) if self.unit == "B" else "%d" % value

    def _emit(self, line, final=False):
        stream = sys.stderr
        try:
            tty = stream.isatty()
        except (AttributeError, ValueError):
            tty = False
        if tty:
            stream.write("\r  %s%s" % (line, "\n" if final else ""))
        else:
            stream.write("  %s\n" % line)
        stream.flush()

    def update(self, delta=1, force=False):
        """累加进度；达到间隔（或 force）时输出一行。"""
        self.count += delta
        now = time.time()
        if not force and now - self._last_emit < self.interval:
            return
        self._last_emit = now
        if self.total:
            body = "%s / %s (%.0f%%)" % (self._fmt(self.count), self._fmt(self.total),
                                          100.0 * self.count / self.total)
        else:
            body = self._fmt(self.count)
        self._emit("%s: %s" % (self.label, body))

    def finish(self):
        """输出收尾行（累计量、用时、平均速率）。"""
        elapsed = max(time.time() - self.started, 1e-6)
        self._emit("%s: 完成 %s（用时 %.1fs，平均 %s/s）" % (
            self.label, self._fmt(self.count), elapsed, self._fmt(self.count / elapsed)),
            final=True)


# ---------------------------------------------------------------- 配置读写

def _stderr_write(data):
    """把原始字节写到 stderr（保留 git 进度条的 \r 刷新）。"""
    buf = getattr(sys.stderr, "buffer", None)
    if buf is None:
        sys.stderr.write(data.decode("utf-8", "replace"))
        sys.stderr.flush()
        return
    buf.write(data)
    buf.flush()


def run(cmd, cwd=None, check=False, capture=True, timeout=None, echo=False, heartbeat=10):
    """执行命令。返回 (returncode, stdout, stderr)；check=True 时非 0 直接抛异常。

    echo=True 时改用 run_live()：把子进程 stderr（git 进度条、日志）实时转发到终端，
    避免长命令（clone / 下载 / 解压）看起来像卡死；stdout 仍被捕获并返回。
    """
    if echo:
        return run_live(cmd, cwd=cwd, check=check, timeout=timeout, heartbeat=heartbeat)
    proc = subprocess.run(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        text=True,
        timeout=timeout,
    )
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if check and proc.returncode != 0:
        raise WsError("CMD_FAILED", "命令执行失败: %s\n%s" % (" ".join(cmd), err or out))
    return proc.returncode, out, err


def run_live(cmd, cwd=None, check=False, timeout=None, heartbeat=10):
    """执行命令并实时转发其 stderr，返回 (returncode, stdout, stderr)。

    - stdout 单独捕获（保证子脚本的 JSON 协议不被进度输出污染）
    - stderr 原样转发到终端，兼容 git 的 `\r` 进度刷新
    - 超过 `heartbeat` 秒无任何输出时打一条 INFO 心跳，提示仍在进行
    - `timeout` 为总时长上限，超时杀进程并抛 WsError
    """
    proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, bufsize=0)
    out_buf, err_buf = bytearray(), bytearray()
    out_fd, err_fd = proc.stdout.fileno(), proc.stderr.fileno()
    open_fds = {out_fd: out_buf, err_fd: err_buf}
    started = time.time()
    last_out = started
    timed_out = False
    try:
        while True:
            if timeout is not None and time.time() - started > timeout:
                timed_out = True
                proc.kill()
                break
            fds = list(open_fds)
            if not fds:
                break
            ready, _, _ = select.select(fds, [], [], heartbeat)
            if not ready:
                if proc.poll() is not None:
                    continue
                if time.time() - last_out >= heartbeat:
                    last_out = time.time()
                    log("%s 仍在进行（已等待 %ds）" % (cmd_label(cmd), int(time.time() - started)),
                        "INFO")
                continue
            for fd in ready:
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    chunk = b""
                if not chunk:
                    open_fds.pop(fd, None)
                    continue
                open_fds[fd].extend(chunk)
                if fd == err_fd:
                    _stderr_write(chunk)
                last_out = time.time()
    finally:
        if proc.poll() is None:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        for stream in (proc.stdout, proc.stderr):
            try:
                stream.close()
            except OSError:
                pass

    out = out_buf.decode("utf-8", "replace").strip()
    err = err_buf.decode("utf-8", "replace").strip()
    if timed_out:
        raise WsError("CMD_TIMEOUT", "命令超时（%ss）: %s" % (timeout, cmd_label(cmd)))
    if check and proc.returncode != 0:
        raise WsError("CMD_FAILED", "命令执行失败: %s\n%s" % (cmd_label(cmd), err or out))
    return proc.returncode, out, err


class WsError(Exception):
    """带错误码的受控异常。"""

    def __init__(self, code, message, **extra):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


def is_ws_root(path):
    """判断 path 是否为已初始化的工作空间根。

    仅看 `.ws_env/` 目录不够：祖先目录里若有残留的 `.ws_env`（例如仓库群
    根目录、home 目录）会把工作区根误判到上层，导致文件写到错误位置。
    这里以 `ws.config` 为初始化完成的标志（它由 init 在下载/解析成功后写）。
    """
    return os.path.isfile(os.path.join(path, ENV_DIR_NAME, CONFIG_NAME))


def has_env_dir(path):
    """path 下是否存在 .ws_env 目录（不要求已完成初始化）。

    用于 `render_final` 这类需要在 init 失败场景也能落盘的场合：
    此时 `.ws_env` 已由 init 开头创建，但 `ws.config` 尚未写入。
    """
    return os.path.isdir(os.path.join(path, ENV_DIR_NAME))


def find_ws_root(start=None):
    """向上查找已初始化的 .ws_env 所在目录作为工作空间根；找不到返回起始目录。"""
    start = os.path.abspath(start or os.getcwd())
    cur = start
    while True:
        if is_ws_root(cur):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return start
        cur = parent


def env_dir(root=None):
    return os.path.join(root or find_ws_root(), ENV_DIR_NAME)


def parse_config_text(text):
    """解析 shell 风格配置：KEY = value; 支持 # 注释与引号。"""
    cfg = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().upper()
        value = value.strip().rstrip(";").strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        cfg[key] = value
    return cfg


def global_config_path():
    """全局配置路径：$XDG_CONFIG_HOME/wsm/ws.config，缺省 ~/.config/wsm/ws.config。"""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join("~", ".config")
    return os.path.expanduser(os.path.join(base, GLOBAL_CONFIG_SUBDIR, CONFIG_NAME))


def load_global_config():
    """读全局配置；文件不存在或不可读时返回空 dict。"""
    try:
        with open(global_config_path(), "r", encoding="utf-8") as fh:
            return parse_config_text(fh.read())
    except OSError:
        return {}


def _load_local_config(root=None):
    path = os.path.join(env_dir(root), CONFIG_NAME)
    if not os.path.isfile(path):
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        return parse_config_text(fh.read())


def load_config(root=None):
    """合并后的配置视图。

    REPOS_ROOT 与用户名（USER_NAME，别名 REPOS_USER_NAME）已改为全局管理：
    以 ~/.config/wsm/ws.config 为准，本地 .ws_env/ws.config 里的同名旧键
    仅作兜底（旧版本创建的工作区仍可用）；其余键本地优先。
    """
    global_cfg = load_global_config()
    cfg = dict(global_cfg)
    cfg.update(_load_local_config(root))
    for key in GLOBAL_FIRST_KEYS:
        if global_cfg.get(key):
            cfg[key] = global_cfg[key]
    return cfg


def save_config(cfg, root=None):
    """写本地 ws.config（shell 风格，分号结尾）。不触碰全局配置文件。"""
    path = os.path.join(env_dir(root), CONFIG_NAME)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lines = ["# 工作空间配置（由 ws.py init 生成，可手工修改）"]
    for key in sorted(cfg):
        lines.append("%s = %s;" % (key, cfg[key]))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return path


def update_config(patch, root=None):
    # 只改本地文件：全局配置（REPOS_ROOT / 用户名）需手工编辑 ~/.config/wsm/ws.config
    cfg = _load_local_config(root)
    cfg.update({k.upper(): v for k, v in patch.items() if v is not None})
    return save_config(cfg, root)


def config_value(key, root=None, default=None):
    return load_config(root).get(key.upper(), default)


def config_user_name(root=None):
    """取用户名：USER_NAME 优先，兼容全局配置的别名 REPOS_USER_NAME。"""
    cfg = load_config(root)
    if cfg.get("USER_NAME"):
        return cfg["USER_NAME"]
    return cfg.get(USER_NAME_ALT_KEY)


# ---------------------------------------------------------------- JSON 数据

def load_json(path, default=None):
    if not os.path.isfile(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (ValueError, OSError):
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
    return path


def load_repos(root=None):
    return load_json(os.path.join(env_dir(root), REPOS_JSON_NAME), {}) or {}


def load_worktrees(root=None):
    data = load_json(os.path.join(env_dir(root), WORKTREES_JSON_NAME), {}) or {}
    data.setdefault("items", {})
    return data


def save_worktrees(data, root=None):
    return save_json(os.path.join(env_dir(root), WORKTREES_JSON_NAME), data)


# ---------------------------------------------------------------- 日志

LOG_LINE_FMT = "%s [%-5s] %s"


def log_dir(root=None):
    """日志目录：<工作区根>/log；未初始化工作空间时退到当前目录。"""
    return os.path.join(root or find_ws_root(), LOG_DIR_NAME)


def log_path(root=None, name=LOG_NAME):
    return os.path.join(log_dir(root), name)


def final_path(root=None):
    """结果汇总 final.md 的路径（与日志同目录）。"""
    return os.path.join(log_dir(root), FINAL_NAME)


def log(msg, level="INFO", root=None):
    """记录一条日志。

    - 始终回显到 stderr
    - WARN / ERROR 额外追加到 <工作区根>/log/ws.log
    - 写入失败不影响主流程
    """
    if level in LOG_LEVELS:
        line = LOG_LINE_FMT % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), level, msg)
        try:
            path = log_path(root)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass
    sys.stderr.write("%s: %s\n" % (level, msg))
    sys.stderr.flush()


# ---------------------------------------------------------------- 子脚本调用

def call_script(script, args, root=None):
    """调用 scripts/ 下的子脚本，返回其 JSON 结果；失败抛 WsError。

    子脚本的 stderr 主要是**进度/心跳**（INFO 级，属于正常输出），
    因此只在其中出现真正的 WARN/ERROR 前缀时才落到日志，避免把进度写成告警。
    """
    cmd = [sys.executable, os.path.join(SCRIPT_DIR, script)] + list(args)
    code, out, err = run(cmd, echo=True)
    for line in (err or "").splitlines():
        stripped = line.strip()
        if stripped.startswith(("WARN", "ERROR")):
            log("%s: %s" % (script, stripped), "WARN", root)
    if code != 0:
        detail = err or out or ("exit=%d" % code)
        raise WsError("SUB_SCRIPT_FAILED", "%s 执行失败: %s" % (script, detail))
    try:
        return json.loads(out) if out else {}
    except ValueError:
        raise WsError("SUB_SCRIPT_BAD_OUTPUT", "%s 未输出合法 JSON: %s" % (script, out[:200]))


# ---------------------------------------------------------------- 文本解析工具

def base_name(path):
    """从 URL 或路径中取最后一段（去 query 与 .git）。"""
    text = (path or "").strip().rstrip("/")
    text = text.split("?", 1)[0].split("#", 1)[0]
    name = text.rsplit("/", 1)[-1]
    if name.endswith(".git"):
        name = name[:-4]
    return name


def repo_name_from_git(url):
    """git 仓库地址 -> 仓库名。支持 https://host/a/b.git 与 git@host:a/b 两种写法。"""
    text = (url or "").strip().rstrip("/")
    m = re.match(r"^[^/@\s]+@[^/:\s]+:(.+)$", text)   # scp 风格 git@host:path
    if m:
        text = m.group(1)
    return base_name(text)


# svn 地址中的组织代号段，分支号从该段开始、到 /code 之前结束
SVN_ORGS = ("cbg", "yjy", "ngsa", "xjy", "cbgtest")

# 仓库名 = 组织代号段之前的部分
# 普通形态 head=[<父容器>, <仓库名>] -> 取最后一段（如 switch_sda_xgs/sda_pktd -> switch_sda_xgs）
# 以下前缀需与后一段拼接：dcn_app/route -> dcn_app_route
JOIN_CONTAINERS = ("dcn_app",)

SIBLING_LETTERS = ("e", "p", "r", "s", "u", "v", "w")


def svn_segments(url):
    """svn 地址 -> ssvn<N>/ 之后的路径段列表。"""
    m = re.search(r"/ssvn\d+/(.+)$", url or "")
    if not m:
        raise WsError("SVN_URL_UNPARSED", "无法从 svn 地址解析路径段: %s" % url)
    return [s for s in m.group(1).split("/") if s]


def svn_org_index(segs):
    """定位组织代号段下标；找不到返回 None。"""
    for i, seg in enumerate(segs):
        if seg.lower() in SVN_ORGS:
            return i
    return None


def svn_repo_name(segs, org_idx):
    """组织代号段之前的部分 -> 仓库名。

    - 普通形态 [<父容器>, <仓库名>] -> 取最后一段（switch_sda_xgs/sda_pktd -> switch_sda_xgs）
    - 聚合容器 [dcn_app, route] -> dcn_app_route
    - 兄弟仓库 [rg-protobuf, s] -> rg_protobuf_s
    """
    if org_idx is None:
        raise WsError("SVN_URL_UNPARSED",
                      "svn 地址中未找到组织代号段（无法映射到 git 分支）: %s" % "/".join(segs))
    head = segs[:org_idx]
    if not head:
        raise WsError("SVN_URL_UNPARSED", "svn 地址中组织代号前无仓库名: %s" % "/".join(segs))
    if len(head) >= 2 and len(head[-1]) == 1 and head[-1].lower() in SIBLING_LETTERS \
            and container_base(head[-2]):
        return "rg_protobuf_%s" % head[-1].lower()
    if head[0].lower() in JOIN_CONTAINERS:
        return "_".join(head)
    return head[-1]


def svn_branch(segs, org_idx):
    """组织代号段 -> /code 之前 -> 分支号，如 cbg/custom/1.27.116.1。"""
    if org_idx is None:
        raise WsError("SVN_URL_UNPARSED",
                      "svn 地址中未找到组织代号段（无法映射到 git 分支）: %s" % "/".join(segs))
    code_idx = segs.index("code") if "code" in segs else len(segs)
    if org_idx >= code_idx:
        raise WsError("SVN_URL_UNPARSED", "svn 地址组织代号在 /code 之后: %s" % "/".join(segs))
    return "/".join(segs[org_idx:code_idx])


def parse_svn_identity(url, component=None):
    """svn 地址 -> (仓库名, 分支号)。

    分支号 = 组织代号段 到 /code 之前，如 cbg/custom/1.27.116.1。
    仓库名 = 组织代号段之前的部分（普通取末段，聚合容器拼接）。

    例：
      .../ssvn12/switch_route/cbg/custom/1.27.116.1/code          -> switch_route,   cbg/custom/1.27.116.1
      .../ssvn12/switch_sda_xgs/cbg/custom/1.27.116.1/code/sda_pktd -> switch_sda_xgs, cbg/custom/1.27.116.1
      .../ssvn12/dcn_app/route/cbg/custom/1.27.116.1/code        -> dcn_app_route,  cbg/custom/1.27.116.1
      .../ssvn12/dcn_app/oob/cbg/custom/1.27.116.1/code          -> dcn_app_oob,    cbg/custom/1.27.116.1
      .../ssvn12/ppf_rg_utils/build_script/yjy/custom/1.27.116.1/code -> build_script, yjy/custom/1.27.116.1
      .../ssvn12/ppf_libpub/rg-protobuf/s/cbg/custom/3.3.0/code  -> rg_protobuf_s,  cbg/custom/3.3.0
    """
    segs = svn_segments(url)
    org_idx = svn_org_index(segs)
    name = svn_repo_name(segs, org_idx)
    branch = svn_branch(segs, org_idx)
    # 兄弟仓库在 svn 里共用同一分支，映射到 git 后需带上字母后缀
    return split_sibling(name, branch)


def git_path(url):
    """从 git 地址中取出路径部分。"""
    text = (url or "").strip().rstrip("/")
    m = re.match(r"^[^/@\s]+@[^/:\s]+:(.+)$", text)
    if m:
        return m.group(1)
    for scheme in ("https://", "http://", "ssh://", "git://"):
        if text.startswith(scheme):
            rest = text[len(scheme):]
            return rest.split("/", 1)[1] if "/" in rest else ""
    return text


def container_base(name):
    """判断是否是兄弟仓库容器名（rg-protobuf / rg_protobuf）。"""
    key = re.sub(r"[-_]", "", (name or "").lower())
    return "rg_protobuf" if key == "rgprotobuf" else None


def split_sibling(repo_name, branch):
    """兄弟仓库容器：rg_protobuf/<字母>/<分支> -> (rg_protobuf_<字母>, <分支>)。

    仅在容器名为 rg_protobuf 且首段确为单字母时才改写，
    避免误伤 cbg/yjy 等组织代号开头的分支。
    """
    if not branch or container_base(repo_name) is None:
        return repo_name, branch
    head, _, rest = branch.partition("/")
    if len(head) == 1 and head.lower() in SIBLING_LETTERS and rest:
        return "rg_protobuf_%s" % head.lower(), rest
    return repo_name, branch


def git_path_for_sibling(url):
    """git 地址中的兄弟仓库识别：rg_protobuf/<字母> -> rg_protobuf_<字母>。"""
    parts = [p for p in git_path(url).split("/") if p]
    if len(parts) >= 2 and container_base(parts[-2]) and \
            len(parts[-1]) == 1 and parts[-1].lower() in SIBLING_LETTERS:
        return "rg_protobuf_%s" % parts[-1].lower()
    return None


def parse_repo_identity(repo_url, branch=None, repo_type="git"):
    """解析仓库地址，返回 (仓库名, 分支号)。"""
    if repo_type == "svn":
        return parse_svn_identity(repo_url)
    name = git_path_for_sibling(repo_url) or repo_name_from_git(repo_url)
    return split_sibling(name, branch)


def norm_repo_key(name):
    """仓库名归一化：连字符/下划线等价；rg_protobuf_<xx> 归为 rg_protobuf。"""
    key = re.sub(r"[-_]", "", (name or "").lower())
    if key.startswith("rgprotobuf"):
        return "rgprotobuf"
    return key


def sibling_letter(name):
    """识别 rg_protobuf 兄弟仓库名，返回字母后缀（无则 None）。

    兼容 `rg_protobuf_r` 与 `rg-protobuf-r` 两种写法（如 `rg_protobuf_e` -> `e`）。
    单独一个字母必须是 SIBLING_LETTERS 之一，避免把 `rg_protobuf_x_y` 之类误判。
    """
    m = re.match(r"^rg[-_]protobuf[-_]([a-z])$", (name or "").strip(), re.IGNORECASE)
    if not m:
        return None
    letter = m.group(1).lower()
    return letter if letter in SIBLING_LETTERS else None


def is_protobuf_tool(name):
    """识别 rg-protobuf 的工具仓库（rg-protobuf-tool / rg_protobuf_tool）。

    这类仓库在 gerrit 上是**独立仓库**（`rg-protobuf-tool`），但 repos.json
    只记容器 `rg-protobuf`，因此分支号要从容器记录取。
    注意 `norm_repo_key("rg-protobuf-tool")` 是 `rgprotobuftool`，与容器的
    `rgprotobuf` 不同，所以必须在归一化分支之前单独处理。
    """
    return re.sub(r"[-_]", "", (name or "").strip().lower()) == "rgprotobuftool"


def now_str():
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- 通用工具

def ensure_dir(path):
    os.makedirs(path, exist_ok=True)
    return path


def confirm(prompt, assume_yes=False):
    """交互确认；assume_yes 为真则直接通过，非交互环境默认拒绝。"""
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        sys.stdout.write("%s [非交互环境，未确认，已跳过]\n" % prompt)
        return False
    sys.stdout.write("%s [y/N] " % prompt)
    sys.stdout.flush()
    try:
        return sys.stdin.readline().strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False
