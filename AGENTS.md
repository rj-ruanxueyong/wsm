# AGENTS.md

RGOS 工作空间管理 CLI：从编译产物（product_info / rootfs.config）解析组件→仓库映射，
批量克隆/建立 worktree、下载镜像包。纯 Python 3 标准库，无第三方依赖，无自动化测试。
详细用法见 `ws.py` 模块 docstring 及各子命令定义（`build_parser`）。

## 文件结构与调用关系

| 文件 | 职责 |
|---|---|
| `ws.py` | 用户唯一入口 CLI（init/add/remove/status/list/doctor），git 操作与状态管理 |
| `wslib.py` | 公共库：JSON 协议、日志、子进程、配置读写、URL 解析，被其余三个脚本 import |
| `download.py` | 统一下载脚本（product_info / rootfs.config / image tar） |
| `extract.py` | 统一解析/解压脚本（生成 repos.json、解压镜像包） |

- `ws.py` 通过 `ws.call_script()` 以 subprocess 调 `download.py` / `extract.py`。
- 修改 `wslib.py` 时必须考虑 `download.py`、`extract.py`、`ws.py` 三个调用方。

## 核心约定（改动前必读）

1. **stdout 只放结果 JSON**（`ws.emit()`）：download/extract 的结果写 stdout，
   进度与日志一律走 stderr（保 `\r` 进度刷新）。`ws.call_script()` 只解析 stdout
   JSON，混入其它输出会直接破坏协议（`SUB_SCRIPT_BAD_OUTPUT`）。
2. **受控失败只走 `wslib.WsError(code, message)`**：批量步骤用 `run_step()` 包裹
   （记录问题、返回 `(ok, value)`，保证批量不中断）；顶层 `main()` 捕 `WsError` 后
   `ws.emit_error()` 输出失败 JSON 并 exit 1。不要裸抛异常或 print 报错。
   - 约定：WARN 不算失败不阻塞；ERROR 算失败。错误码英文大写蛇形
     （`FETCH_FAILED`、`TARGET_EXISTS`…）。
3. **日志策略**：stderr 回显一切；只有 WARN/ERROR 落 `ws_tmp/log/ws.log`；
   `ws_tmp/log/issues.json` 记问题（`add_issue()` 同步写两处）；
   `ws_tmp/log/final.md` 覆盖写汇总。INFO 永不落盘。
4. **状态文件一律原子写**：用 `ws.save_json()` / `ws.save_config()`（.tmp +
   `os.replace`），不要直接 open("w")。
5. **两个根判定不可混用**：`ws.is_ws_root()`（严格，须有 `.ws_env/ws.config`）用于
   `find_ws_root()`；`ws.has_env_dir()`（宽松）仅用于 `render_final()` 的守卫——
   init 失败时也要产出 final.md 便于定位。

## 代码风格

- 注释/日志/docstring 用中文；常量集中在 `wslib.py` 头部，大写蛇形。
- 字符串格式化用 `%`，**不要用 f-string**；无类型注解（保持既有风格）。
- 4 空格缩进，行长 ≤80 列。
- 命名：命令函数 `cmd_*`，读写 `load_*` / `save_*`，解析 `parse_*`。
- 文件头：`#!/usr/bin/env python3` + `# -*- coding: utf-8 -*-` + 中文 docstring。

## 状态与数据

- `.ws_env/`：`ws.config`（shell 风格 `KEY = value;`）、`repos.json`
  （组件→仓库映射，由 extract.py 生成）、`worktrees.json`、`images.json`。
- 全局配置 `~/.config/wsm/ws.config`（格式同 `ws.config`，`XDG_CONFIG_HOME` 可改位置）：
  承载跨工作区共享的 `REPOS_ROOT` 与用户名（`USER_NAME`，别名 `REPOS_USER_NAME`）。
  这两个键**全局优先**，本地 `.ws_env/ws.config` 的同名旧键仅作兜底；
  init 不再把它们写进本地 ws.config，修改要改全局文件。
- `ws_tmp/`：下载中间产物（product_info / rootfs.config / image tar）；
  下方 `ws_tmp/log/`：`ws.log`、`issues.json`、`final.md`。
  所有 JSON `ensure_ascii=False`。目录可整体删除，下次命令按需重建。
- `find_ws_root()` 上溯找不到根时返回**起始 cwd**，不允许写状态到 `/`。

## 验证方式（仓库内无测试）

- 改动后先跑非破坏性命令核对 stdout JSON 与 `ws_tmp/log/final.md`：
  `python3 ws.py doctor`、`python3 ws.py status`、`python3 ws.py list`。
- init/add 等联网命令可用离线夹具验证：临时目录造 bare 远端 + 手写最小
  `.ws_env/{ws.config,repos.json}`（repos.json 走真实格式、REPOS_ROOT 指向假目录）。
- git 相关改动注意本机 git 较老，不要用新版本才有的参数（如 `git init -b`）。

## 环境坑位

- **gerrit SSH 只认自家命令**：连通性探测用 `gerrit version`，不能用 `true`。
- **整体 gerrit clone 会被数千 refs/changes 拖死**：主克隆默认 fast fetch 单分支
  （`git init` + `fetch refs/heads/<分支>`）；`--no-fast` 才整体 clone。
- **判断分支被 worktree 占用**一律用 `git worktree list --porcelain` 反查，
  不要用 `show-ref`（只看分支存在）。
- 外部解析文件读写用 `encoding="utf-8", errors="replace"`（rootfs.config 不可信）。
- 镜像包格式不可信：按文件头魔数识别 gzip/bz2（`*.tar.bz2` 实为 gzip 属常见情况）；
  解压有 zip-slip 防护（`safe_extract`），不要绕过。
- `rg-protobuf-tool` 与 `rg_protobuf_<字母>` 归一化键不同，查找时须先单独拦截
  （`is_protobuf_tool`），否则会误配到容器仓库。
