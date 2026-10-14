# wsm

RGOS 工作空间管理 CLI：从编译产物（product_info / rootfs.config）解析组件→仓库映射，
批量建立 git worktree、下载镜像包。纯 Python 3 标准库实现，无第三方依赖。

- 入口：`ws.py`（唯一用户命令）
- 子脚本：`download.py`（统一下载）、`extract.py`（统一解析/解压）
- 公共库：`wslib.py`（JSON 协议、日志、子进程、配置读写）

---

## 1. 环境要求

| 项 | 要求 |
| --- | --- |
| Python | 3.x（标准库即可） |
| git | 已安装并加入 PATH（本机 git 较旧也可用） |
| gerrit | 已配置 SSH 公钥，能访问 `gerrit.ruijie.work:29418` |

自检：

```bash
python3 ws.py doctor          # 检查 git / 配置 / 仓库索引
python3 ws.py doctor --net    # 额外检查 gerrit 连通性（用 gerrit version 探测）
```

---

## 2. 全局配置（一次配置，所有工作区共用）

仓库群根路径 `REPOS_ROOT` 与 git 用户名 `USER_NAME` 读取全局配置文件
`~/.config/wsm/ws.config`（可用 `XDG_CONFIG_HOME` 改位置，即
`$XDG_CONFIG_HOME/wsm/ws.config`）：

```bash
mkdir -p ~/.config/wsm
cat > ~/.config/wsm/ws.config <<'EOF'
REPOS_ROOT = /codespace/workspace/repos;
USER_NAME = ruanxueyong;
EOF
```

说明：

- 格式为 shell 风格，每行 `KEY = value;`（分号结尾），支持 `#` 注释与引号。
- 用户名可用别名 `REPOS_USER_NAME` 代替 `USER_NAME`，两者皆可识别（
  `USER_NAME` 优先）。
- **全局优先**：如果旧工作区本地 `.ws_env/ws.config` 里还残留同名
  `REPOS_ROOT` / `USER_NAME`，会被全局值覆盖，本地旧键仅作兜底（
  无全局文件时仍生效）。
- 其余键（`PROJECT`、`BUILD_PROJECT`、`COMPILE_OUTPUT_URL`、`IMAGES_URL`
  等工作区属性）只保存在各工作区本地，与全局无关。
- 修改仓库根 / 用户名一律改全局文件；`init` 不会再把它们写入本地。

---

## 3. 快速上手（典型流程）

```bash
# 1) 初始化工作区：下载编译产物信息并解析出 组件→仓库 映射
python3 ws.py init --url <项目 compile_output 链接>

# 2) 批量建立 worktree（默认快速拉取：单分支 fetch，比整体 clone 快得多）
python3 ws.py add --repo switch_route --repo switch_bridge

# 3) 获取 images 镜像包（可选；--header 只保留 header 目录）
python3 ws.py add --img

# 4) 查看状态 / 索引 / 自检
python3 ws.py status
python3 ws.py list --all
python3 ws.py doctor
```

---

## 4. 命令参考

### 4.1 `init` — 初始化工作空间

```bash
ws.py init --url <prj_compile_output_url> [--repos-root <path>] [--user <user>]
```

- 下载并解析 product_info / rootfs.config，生成 `.ws_env/repos.json`
  （组件→仓库映射），写入本地 `ws.config`（不含 REPOS_ROOT / 用户名）。
- `--repos-root` / `--user`：临时覆盖，**缺省读全局配置**；不传用户名时
  全局也没有则尝试从 `git config user.email` 推导，仍失败报错
  （提示配置全局 `USER_NAME`）。

### 4.2 `add` — 导入仓库分支 / 获取 images

```bash
ws.py add --repo <仓库名> [--repo <仓库名> ...] [--no-fast]
ws.py add --img [--url <url>] [--path <dir>] [--force]
ws.py add --header --url <url> [--force]
```

- `--repo` 可重复，为每个仓库建立 worktree：
  - 目录：`<工作区根>/<仓库名>`；
  - 本地分支：`<工作区目录名>_<仓库名>`（如 `test1_switch_route`），
    同一主克隆可在多工作区并存同仓库。
- 主克隆默认 **快速拉取**：`git init` + `fetch --no-tags origin
  refs/heads/<分支号>`，只拉目标分支、保留完整历史，比整体 clone 快
  一个量级；`--no-fast` 才整体 clone。
- `--img` / `--header` 与 `--repo` 二选一场景使用；`--force` 覆盖已存在
  的 images 链接/目录；`--url` 缺省从 `repos.json` 取优先配置。

### 4.3 `remove` — 移除 worktree

```bash
ws.py remove --repo <仓库名> [--yes]
ws.py remove --all [--yes]
```

- 移除 worktree 并 `git worktree prune`，连带删除本地分支
  （分支名与工作区绑定，不影响他人）。
- 分支仍被其它 worktree 占用 → 告警跳过；有未推送提交**不阻止**删除
  （按设计如此，删除前请自行确认）。
- `--yes` 跳过交互确认。

### 4.4 `list` — 查看索引

```bash
ws.py list [--repo | --component | --all]
```

- `--repo` 按仓库名、`--component` 按组件名列出映射；
  `--all` 列出全部仓库索引（含分支号）。

### 4.5 `status` — 查看并刷新状态

```bash
ws.py status
```

- stdout 输出结果 JSON（worktree 状态、config 解析值、images、问题记录），
  同时覆盖刷新 `.ws_tmp/log/final.md` 汇总（失败场景也会产出，便于定位）。

### 4.6 `doctor` — 自检

```bash
ws.py doctor [--net]
```

- 检查 git、`.ws_env` 初始化、配置可读、**全局配置存在/含用户名**、
  REPOS_ROOT 已配置且存在、仓库索引非空等；
  `--net` 额外探测 gerrit 连通性。

---

## 5. 数据与日志文件

```
<工作区根>/
├── .ws_env/
│   ├── ws.config        # 本地工作区配置（shell 风格 KEY = value;）
│   ├── repos.json       # 组件→仓库映射（init 生成）
│   ├── worktrees.json   # worktree 登记（add/remove 维护）
│   └── images.json      # images 获取记录
└── .ws_tmp/             # 临时产物目录（可整体删除，下次命令按需重建）
    ├── *.tar 等          # 下载的编译产物 / images 包（中间产物）
    └── log/
        ├── ws.log        # 只记 WARN/ERROR（INFO 仅回显 stderr）
        ├── issues.json   # 问题记录（步骤/对象/错误码）
        └── final.md      # 结果汇总（每次 status/批量操作后覆盖刷新）
```

- 结果 JSON 只走 stdout；进度与日志一律走 stderr。
- 全局配置：`~/.config/wsm/ws.config`（REPOS_ROOT 与用户名仅此一处维护）。

---

## 6. 常见问题（FAQ）

- **克隆很久没反应 / 很慢？**
  新版 `add` 默认就是单分支快速拉取（`--fast` 为显式写法）。若仍慢，
  可用 `ssh -p 29418 <user>@gerrit.ruijie.work git-upload-pack '<repo>'
  | wc -c` 排查服务端与网络通道。
- **`add --repo xxx` 报分支被占用？**
  说明该本地分支已被其它 worktree 占用，先在对应工作区 `remove`，
  或换个仓库名/工作区名（分支名含工作区目录名前缀，天然隔离）。
- **doctor 报全局配置未找到？**
  按第 2 节创建 `~/.config/wsm/ws.config`（注意分号结尾）。
- **image 包报 "not a bzip2 file"？**
  正常，脚本按魔数自动识别 gzip/bz2；包内多余的 `images/` 嵌套目录
  会自动扁平化。
