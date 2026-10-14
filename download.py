#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""download.py - 统一下载脚本。

用法：
    download.py product --url <output_url> --out <dir>
        下载 product_info 与 rootfs.config 到 <dir>。

    download.py image   --url <url> --out <dir>
        下载镜像包（直链）到 <dir>。

    download.py image   --base <output根> --build-project <bp> --project <prj>
                       --out <dir>
        在 <base>/<build_project>/<project>/ 下，按构建目录名中的
        id<时间戳> 从新到旧，探测首个含 image.tar.bz2 的目录并下载。

约定：结果 JSON 写 stdout；失败时写 {"ok": false, ...} 并返回非 0。
"""

import argparse
import os
import re
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wslib as ws  # noqa: E402

TIMEOUT = 120
CHUNK = 1024 * 256
UA = "rgos-workspace-builder"
HEADERS = {"User-Agent": UA}
IMAGE_NAME = "image.tar.bz2"          # 项目构建目录下的镜像包固定名


def http_get(url, timeout=TIMEOUT):
    """GET 一个 URL，返回 bytes。"""
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except (urllib.error.URLError, OSError) as exc:
        raise ws.WsError("HTTP_FAILED", "请求失败 %s: %s" % (url, exc))


def download(url, out_path, timeout=TIMEOUT, label=None):
    """流式下载到 out_path，返回字节数。

    带进度：有 Content-Length 时显示 "已传 / 总量 (百分比)"。
    产物先写 <path>.part，成功后再原子替换。
    """
    ws.ensure_dir(os.path.dirname(out_path) or ".")
    req = urllib.request.Request(url, headers=HEADERS)
    total = 0
    tmp = out_path + ".part"
    label = label or os.path.basename(out_path)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            size = resp.getheader("Content-Length")
            total_size = int(size) if size and size.isdigit() else None
            prog = ws.Progress("下载 %s" % label, total=total_size)
            ws.log("正在下载 %s（%s）" % (label, ws.human_size(total_size)), "INFO")
            with open(tmp, "wb") as fh:
                while True:
                    chunk = resp.read(CHUNK)
                    if not chunk:
                        break
                    fh.write(chunk)
                    total += len(chunk)
                    prog.update(len(chunk))
            prog.finish()
    except (urllib.error.URLError, OSError) as exc:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise ws.WsError("DOWNLOAD_FAILED", "下载失败 %s: %s" % (url, exc))
    os.replace(tmp, out_path)
    return total


def pick_image_name(url):
    """镜像文件名：URL 最后一段有扩展名就用它，否则用 image.tar.bz2。"""
    name = ws.base_name(url)
    if name and ("." in name):
        return name
    return IMAGE_NAME


def scan_info_dirs(base):
    """尝试列举目录，返回其中 *_info 目录名列表（不支持列举时返回空）。"""
    try:
        html = http_get(base).decode("utf-8", "replace")
    except ws.WsError:
        return []
    hrefs = set(re.findall(r'href="([^"?][^"]*?)/?"', html))
    names = []
    for href in hrefs:
        if href.startswith("http") or href.startswith("..") or href.startswith("/"):
            continue
        name = href.rstrip("/").split("/")[-1]
        if name.endswith("_info") and name not in names:
            names.append(name)
    return sorted(names)


def build_info_candidates(output_url, explicit, listing):
    """按优先级生成候选 info 目录 URL 列表。"""
    base = output_url.rstrip("/")
    cands = []

    def add(url):
        url = url.rstrip("/")
        if url and url not in cands:
            cands.append(url)

    if explicit:
        add(explicit if explicit.startswith("http") else "%s/%s" % (base, explicit))
    if base.endswith("_info"):
        add(base)
    for name in listing:
        add("%s/%s" % (base, name))

    # 从 output 末段推断：switch_0_A_id2026... -> switch_info
    tail = base.split("/")[-1]
    stem = tail.split("_id", 1)[0]
    if stem:
        add("%s/%s_info" % (base, stem.split("_")[0]))
    for guess in ("switch_info", "rgos_info"):
        add("%s/%s" % (base, guess))
    add(base)  # 兜底：product_info 可能直接放在 output 根目录
    return cands


def fetch_product_info(output_url, out_dir, explicit):
    """按候选目录逐个尝试下载 product_info，返回 (info_url, info_name, record)。"""
    base = output_url.rstrip("/")
    listing = [] if explicit else scan_info_dirs(base)
    attempts = []
    target = os.path.join(out_dir, "product_info")

    for cand in build_info_candidates(output_url, explicit, listing):
        url = "%s/product_info" % cand
        try:
            size = download(url, target)
            name = cand.split("/")[-1]
            return cand, name, {"url": url, "path": target, "size": size}
        except ws.WsError as exc:
            attempts.append("%s -> %s" % (url, exc.message))
            ws.log("候选 product_info 不可用: %s" % url, "WARN")
    raise ws.WsError("PRODUCT_INFO_FAILED",
                     "未能下载 product_info，已尝试：\n  %s" % "\n  ".join(attempts))


def cmd_product(args):
    """下载 product_info 与 rootfs.config。"""
    out_dir = os.path.abspath(args.out)
    ws.ensure_dir(out_dir)

    info_url, info_name, record = fetch_product_info(args.url, out_dir, args.info_dir)
    files = {"product_info": record}

    rootfs_url = "%s/rootfs/rootfs.config" % info_url
    rootfs_path = os.path.join(out_dir, "rootfs.config")
    try:
        size = download(rootfs_url, rootfs_path)
        files["rootfs.config"] = {"url": rootfs_url, "path": rootfs_path, "size": size}
    except ws.WsError as exc:
        ws.emit_error("ROOTFS_FAILED", "未能下载 rootfs.config（%s）: %s" % (rootfs_url, exc.message))

    ws.emit({"ok": True, "action": "product", "out": out_dir,
             "info_url": info_url, "info_name": info_name, "files": files})


def probe_url(url, timeout=TIMEOUT):
    """探测 URL 是否可下载：Range 只读极少字节，不做整包下载。

    404 等错误由 urllib 抛出 HTTPError，统一包成 WsError 供调用方
    对候选目录逐个尝试。服务器不支持 Range 时返回 200 全文，也视为可用。
    """
    req = urllib.request.Request(url, headers=dict(HEADERS, Range="bytes=0-0"))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read(16)
            return resp.getcode()
    except (urllib.error.URLError, OSError) as exc:
        raise ws.WsError("IMAGE_PROBE_FAILED", "探测不可用 %s: %s" % (url, exc))


def dir_stamp(name):
    """构建目录名中的 id<时间戳>（如 id202610132334011），用于新旧排序。"""
    m = re.search(r"id(\d{9,})", name)
    return int(m.group(1)) if m else 0


def list_dir_names(base):
    """列举目录页，返回其中的子目录名列表（仅带尾斜杠的目录链接）。"""
    html = http_get(base).decode("utf-8", "replace")
    names = []
    for href in re.findall(r'href="([^"?][^"]*)"', html):
        if href.startswith(("http://", "https://", "/", "..", "#")):
            continue
        if not href.endswith("/"):
            continue
        name = href[:-1].split("/")[-1]
        if name in ("", ".") or name in names:
            continue
        names.append(name)
    return names


def find_image_url(base, build_project, project, image_name=IMAGE_NAME):
    """在 <base>/<build_project>/<project>/ 下查找最新含镜像包的构建目录。

    构建目录按名字里的 id<时间戳> 从新到旧逐个探测
    <目录>/<image_name>，命中即返回 (镜像直链, 查找信息 dict)。
    """
    trunk = "%s/%s/%s" % (base.rstrip("/"), build_project, project)
    try:
        dirs = list_dir_names(trunk)
    except ws.WsError as exc:
        raise ws.WsError("IMAGE_LIST_FAILED",
                         "无法列举构建目录 %s: %s" % (trunk, exc.message))
    ordered = sorted(dirs, key=lambda n: (dir_stamp(n), n), reverse=True)
    if not ordered:
        raise ws.WsError("IMAGE_NOT_FOUND", "%s 下未列举到任何构建目录" % trunk)
    attempts = []
    for name in ordered:
        url = "%s/%s/%s" % (trunk, name, image_name)
        try:
            probe_url(url)
            return url, {"trunk": trunk, "chosen": name, "candidates": ordered}
        except ws.WsError as exc:
            attempts.append(exc.message)
            ws.log("候选镜像不可用: %s" % url, "WARN")
    raise ws.WsError("IMAGE_NOT_FOUND",
                     "在 %s 下 %d 个构建目录均无 %s：\n  %s"
                     % (trunk, len(ordered), image_name, "\n  ".join(attempts)))


def cmd_image(args):
    """下载镜像包：--url 直链，或按 --base/--build-project/--project 自动查找。"""
    out_dir = os.path.abspath(args.out)
    ws.ensure_dir(out_dir)
    found = None
    url = args.url
    if not url:
        if not (args.base and args.build_project and args.project):
            ws.emit_error("ARG_MISSING",
                          "需要 --url，或同时指定 --base / --build-project / --project")
        url, found = find_image_url(args.base, args.build_project, args.project)
        ws.log("已定位镜像: %s" % url, "INFO")
    name = args.name or pick_image_name(url)
    target = os.path.join(out_dir, name)
    try:
        size = download(url, target)
    except ws.WsError as exc:
        ws.emit_error("IMAGE_DOWNLOAD_FAILED", exc.message)
    ws.emit({"ok": True, "action": "image", "out": out_dir,
             "file": {"url": url, "path": target, "name": name, "size": size},
             "found": found})


def build_parser():
    parser = argparse.ArgumentParser(prog="download.py", description="统一下载脚本")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("product", help="下载 product_info 与 rootfs.config")
    p.add_argument("--url", required=True, help="编译 output 目录链接")
    p.add_argument("--out", required=True, help="下载落地目录")
    p.add_argument("--info-dir", default=None, help="显式指定 *_info 目录（默认自动探测）")
    p.set_defaults(func=cmd_product)

    p = sub.add_parser("image", help="下载镜像包（直链或按构建工程自动查找）")
    p.add_argument("--url", default=None, help="镜像包直链（与 --base 组参数二选一）")
    p.add_argument("--base", default=None, help="编译产物根目录（截至 .../output）")
    p.add_argument("--build-project", default=None, help="构建工程名（如 git-build-sw）")
    p.add_argument("--project", default=None, help="项目名（如 12_5_SPT462）")
    p.add_argument("--out", required=True, help="下载落地目录")
    p.add_argument("--name", default=None, help="保存文件名（默认从 URL 推断）")
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
