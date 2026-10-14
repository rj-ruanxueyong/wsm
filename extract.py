#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""extract.py - 统一解析 / 解压 / 提取脚本。

用法：
    extract.py product --dir <dir> --out <json>
        解析 <dir> 下的 product_info 与 rootfs.config，产出 repos.json。

    extract.py image   --file <tar> --dir <dir> [--header] [--keep-nested]
        解压镜像包到 <dir>（自动识别 bz2 / gzip / 未压缩 tar）。
        --header      解压后只保留 header 目录
        --keep-nested 保留包内嵌套的 images/ 目录（默认扁平化）

约定：结果 JSON 写 stdout；失败时写 {"ok": false, ...} 并返回非 0。
"""

import argparse
import os
import re
import shutil
import sys
import tarfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wslib as ws  # noqa: E402

GZIP_MAGIC = b"\x1f\x8b"
BZIP2_MAGIC = b"BZh"
URL_RE = re.compile(r"https?://[^\s'\"]+")
KV_RE = re.compile(r"(\w+)\s+(\S+)")


# ---------------------------------------------------------------- product_info

def decode_url(raw):
    """还原 URL 中的百分号编码（如 %5bdef%5d -> [def]）。"""
    return re.sub(r"%([0-9a-fA-F]{2})",
                  lambda m: chr(int(m.group(1), 16)), raw)


def parse_product_info(path):
    """从 product_info 提取编译工程 / 项目 / output / images。"""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()

    urls = [u for u in URL_RE.findall(text)]
    if not urls:
        raise ws.WsError("PRODUCT_INFO_UNPARSED", "product_info 中未找到 output 链接: %s" % path)

    # 优先取形如 .../output/<工程>/<项目>/<构建目录> 的链接
    raw = None
    for url in urls:
        if "/output/" in url:
            raw = url
            break
    if raw is None:
        raw = urls[0]
    output_url = raw.rstrip("/")

    segments = [s for s in output_url.split("/") if s]
    build_project, project = None, None
    if "output" in segments:
        idx = len(segments) - 1 - segments[::-1].index("output")
        tail = segments[idx + 1:]
        if len(tail) >= 1:
            build_project = tail[0]
        if len(tail) >= 2:
            project = tail[1]

    return {
        "build_project": build_project,
        "project": project,
        "output": output_url,
        "output_display": decode_url(output_url),
        "images": "%s/image.tar.bz2" % output_url,
        "product_info": os.path.abspath(path),
        "remote_urls": [decode_url(u) for u in urls],
    }


# ---------------------------------------------------------------- rootfs.config

def parse_rootfs_line(line):
    """解析一行 `#@ <组件> key value key value ...`。"""
    body = line.lstrip("#@").strip()
    if not body:
        return None
    parts = body.split()
    if len(parts) < 3:
        return None

    component = parts[0]
    fields = {}
    for key, value in KV_RE.findall(" ".join(parts[1:])):
        fields[key.lower()] = value

    repo_url = fields.get("repo", "")
    if not repo_url:
        return None

    if "commitid" in fields:
        repo_type = "git"
    elif "version" in fields:
        repo_type = "svn"
    else:
        repo_type = "unknown"

    item = {
        "component": component,
        "repo_type": repo_type,
        "repo_url": repo_url,
        "branch": fields.get("branch"),
        "commitid": fields.get("commitid"),
        "version": fields.get("version"),
        "raw": line.strip(),
        "pinned": repo_type == "git",
    }

    try:
        name, branch = ws.parse_repo_identity(repo_url, item["branch"], repo_type)
        item["repo_name"] = name
        item["branch"] = branch
        if repo_type == "svn":
            # svn 组件映射为 git 仓库：version 无法定位提交，只能取分支 tip
            item["pinned"] = False
            item["map_note"] = "svn 组件按 git 分支 tip 检出，无法按 svn version 固定提交"
    except ws.WsError as exc:
        item["repo_name"] = None
        item["parse_error"] = exc.message

    if item["branch"] in (None, "", "NA", "na", "N/A"):
        if repo_type == "git":
            item["branch"] = None
    return item


def parse_rootfs_config(path):
    """扫描 rootfs.config 中所有 `#@` 开头的组件行。"""
    items, errors = [], []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for lineno, line in enumerate(fh, 1):
            if not line.startswith("#@"):
                continue
            item = parse_rootfs_line(line)
            if item is None:
                continue
            if item.get("repo_name") is None:
                errors.append({"line": lineno, "raw": line.strip(),
                               "reason": item.get("parse_error")})
            items.append(item)
    return items, errors


def build_indexes(items):
    """建立仓库名索引（精确名 + 归一化名，rg_protobuf_<xx> 归到 rg_protobuf）。"""
    repos, normalized, conflicts = {}, {}, []

    def put(key, entry, component):
        if key in repos and repos[key]["repo_name"] != entry.get("repo_name"):
            conflicts.append({"key": key, "kept": repos[key]["repo_name"],
                              "dropped": entry.get("repo_name"), "component": component})
            return
        repos[key] = entry

    for item in items:
        name = item.get("repo_name")
        if not name:
            continue
        entry = {
            "repo_name": name,
            "repo_type": item["repo_type"],
            "repo_url": item["repo_url"],
            "branch": item["branch"],
            "commitid": item["commitid"],
            "version": item["version"],
            "pinned": item["pinned"],
            "components": [item["component"]],
        }
        put(name, entry, item["component"])

        norm = ws.norm_repo_key(name)
        if norm in normalized and entry["repo_name"] not in normalized[norm]["repo_names"]:
            normalized[norm]["repo_names"].append(entry["repo_name"])
        elif norm not in normalized:
            normalized[norm] = {"norm_key": norm, "repo_names": [entry["repo_name"]],
                                "branch": entry["branch"], "commitid": entry["commitid"],
                                "repo_type": entry["repo_type"], "repo_url": entry["repo_url"],
                                "pinned": entry["pinned"]}

    # 同一仓库名可能服务多个组件，补齐 components 列表
    for item in items:
        name = item.get("repo_name")
        if name and name in repos:
            comps = repos[name]["components"]
            if item["component"] not in comps:
                comps.append(item["component"])

    return repos, normalized, conflicts


def cmd_product(args):
    src_dir = os.path.abspath(args.dir)
    info_path = args.product_info or os.path.join(src_dir, "product_info")
    rootfs_path = args.rootfs or os.path.join(src_dir, "rootfs.config")
    # 兼容 rootfs.config 放在 rootfs/ 子目录的情况
    if not os.path.isfile(rootfs_path):
        alt = os.path.join(src_dir, "rootfs", "rootfs.config")
        if os.path.isfile(alt):
            rootfs_path = alt
    if not os.path.isfile(info_path):
        ws.emit_error("PRODUCT_INFO_MISSING", "未找到 product_info: %s" % info_path)
    if not os.path.isfile(rootfs_path):
        ws.emit_error("ROOTFS_MISSING", "未找到 rootfs.config: %s" % rootfs_path)

    product = parse_product_info(info_path)
    items, errors = parse_rootfs_config(rootfs_path)
    repos, normalized, conflicts = build_indexes(items)

    data = {
        "schema": 1,
        "created_at": ws.now_str(),
        "product": product,
        "components": {i["component"]: i for i in items},
        "repos": repos,
        "normalized": normalized,
        "issues": {"parse_errors": errors, "conflicts": conflicts},
    }
    out = os.path.abspath(args.out)
    ws.save_json(out, data)
    ws.emit({"ok": True, "action": "product", "out": out,
             "counts": {"components": len(items), "repos": len(repos),
                        "parse_errors": len(errors), "conflicts": len(conflicts)},
             "product": product, "issues": data["issues"]})


# ---------------------------------------------------------------- image

def detect_compression(path):
    """按文件头判断压缩格式。"""
    with open(path, "rb") as fh:
        head = fh.read(4)
    if head.startswith(GZIP_MAGIC):
        return "gzip"
    if head.startswith(BZIP2_MAGIC):
        return "bz2"
    return "none"


def safe_extract(tf, dest):
    """解压并拒绝路径穿越，带进度提示。"""
    dest_abs = os.path.abspath(dest)
    members = []
    for member in tf.getmembers():
        target = os.path.abspath(os.path.join(dest_abs, member.name))
        if not (target == dest_abs or target.startswith(dest_abs + os.sep)):
            raise ws.WsError("UNSAFE_ARCHIVE", "压缩包内含越界路径: %s" % member.name)
        members.append(member)

    prog = ws.Progress("解压", total=len(members), unit="")
    failed = 0
    for member in members:
        try:
            tf.extract(member, dest_abs)
        except (OSError, tarfile.TarError) as exc:
            failed += 1
            ws.log("解压 %s 失败: %s" % (member.name, exc), "WARN")
        prog.update(1)
    prog.finish()
    if failed:
        ws.log("共有 %d 个成员解压失败（已跳过）" % failed, "WARN")
    return len(members)


def hoist_children(src, dst):
    """把 src 下的所有条目上提到 dst，然后删除 src。"""
    for name in os.listdir(src):
        s = os.path.join(src, name)
        d = os.path.join(dst, name)
        if os.path.exists(d):
            shutil.rmtree(d, ignore_errors=True) if os.path.isdir(d) else os.remove(d)
        shutil.move(s, d)
    shutil.rmtree(src, ignore_errors=True)


def flatten_dest(dest):
    """处理多层同名 images 目录，返回所做的调整说明列表。

    1) <dest>/images/images/*  -> <dest>/images/*
    2) <dest> 本身名为 images 且存在 <dest>/images/* -> 上提到 <dest>
    """
    changes = []
    dest = os.path.abspath(dest)

    # 情况 2：目标目录本身就叫 images，包内又有一套 images/
    if os.path.basename(dest.rstrip(os.sep)) == ws.IMAGES_DIR_NAME:
        inner = os.path.join(dest, ws.IMAGES_DIR_NAME)
        if os.path.isdir(inner):
            hoist_children(inner, dest)
            changes.append("%s/%s/* -> %s/*" % (ws.IMAGES_DIR_NAME, ws.IMAGES_DIR_NAME,
                                                ws.IMAGES_DIR_NAME))

    # 情况 1：<dest>/images/images/
    nested = os.path.join(dest, ws.IMAGES_DIR_NAME, ws.IMAGES_DIR_NAME)
    if os.path.isdir(nested):
        hoist_children(nested, os.path.join(dest, ws.IMAGES_DIR_NAME))
        changes.append("images/images/* -> images/*")
    return changes


def list_dirs(path):
    if not os.path.isdir(path):
        return []
    return sorted(n for n in os.listdir(path) if os.path.isdir(os.path.join(path, n)))


def find_header_dir(dest):
    """在解压结果中查找 header 目录（兼容嵌套一层）。"""
    names = ("header", "headers", "Header")
    for name in names:
        path = os.path.join(dest, name)
        if os.path.isdir(path):
            return path
    for sub in list_dirs(dest):
        for name in names:
            path = os.path.join(dest, sub, name)
            if os.path.isdir(path):
                return path
        for sub2 in list_dirs(os.path.join(dest, sub)):
            for name in names:
                path = os.path.join(dest, sub, sub2, name)
                if os.path.isdir(path):
                    return path
    return None


def keep_only_header(dest):
    """只保留 header 目录，其余删除。"""
    header_root = os.path.join(dest, "header")
    found = find_header_dir(dest)
    if found is None:
        raise ws.WsError("HEADER_NOT_FOUND",
                         "镜像包中未找到 header 目录，现有目录: %s" % list_dirs(dest))
    if os.path.abspath(found) != os.path.abspath(header_root):
        if os.path.exists(header_root):
            shutil.rmtree(header_root, ignore_errors=True)
        shutil.move(found, header_root)

    for name in os.listdir(dest):
        if name == "header":
            continue
        target = os.path.join(dest, name)
        shutil.rmtree(target, ignore_errors=True) if os.path.isdir(target) else os.remove(target)
    return header_root


def cmd_image(args):
    tar_path = os.path.abspath(args.file)
    dest = os.path.abspath(args.dir)
    if not os.path.isfile(tar_path):
        ws.emit_error("IMAGE_NOT_FOUND", "未找到镜像包: %s" % tar_path)

    ws.ensure_dir(dest)
    mode = detect_compression(tar_path)

    try:
        if mode == "gzip":
            tf = tarfile.open(tar_path, "r:gz")
        elif mode == "bz2":
            try:
                tf = tarfile.open(tar_path, "r:bz2")
            except tarfile.TarError:
                tf = tarfile.open(tar_path, "r:gz")
        else:
            tf = tarfile.open(tar_path, "r:")
    except (tarfile.TarError, OSError) as exc:
        ws.emit_error("IMAGE_OPEN_FAILED", "无法打开镜像包 %s（判定格式 %s）: %s"
                      % (tar_path, mode, exc))

    with tf:
        count = safe_extract(tf, dest)

    flattened = []
    if not args.keep_nested:
        flattened = flatten_dest(dest)

    header_dir = None
    if args.header:
        header_dir = keep_only_header(dest)

    ws.emit({"ok": True, "action": "image", "file": tar_path, "dir": dest,
             "compression": mode, "members": count,
             "flattened": flattened, "header_dir": header_dir,
             "top_level": list_dirs(dest)})


# ---------------------------------------------------------------- CLI

def build_parser():
    parser = argparse.ArgumentParser(prog="extract.py", description="统一解析 / 解压 / 提取脚本")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("product", help="解析 product_info 与 rootfs.config -> repos.json")
    p.add_argument("--dir", required=True, help="包含 product_info 与 rootfs.config 的目录")
    p.add_argument("--out", required=True, help="repos.json 输出路径")
    p.add_argument("--product-info", default=None, help="显式指定 product_info 路径")
    p.add_argument("--rootfs", default=None, help="显式指定 rootfs.config 路径")
    p.set_defaults(func=cmd_product)

    p = sub.add_parser("image", help="解压镜像包（可只保留 header）")
    p.add_argument("--file", required=True, help="镜像包路径（.tar.bz2/.tar.gz/.tar）")
    p.add_argument("--dir", required=True, help="解压目标目录")
    p.add_argument("--header", action="store_true", help="只保留 header 目录")
    p.add_argument("--keep-nested", action="store_true", help="保留包内嵌套 images/ 目录")
    p.set_defaults(func=cmd_image)
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
