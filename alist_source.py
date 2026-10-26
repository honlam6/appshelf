#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 AList 站点生成 AltStore / SideStore 软件源（source.json）。

特性:
  - 多目录扫描，递归深度可配
  - 并发远程解析 IPA（只读 EOCD/中央目录/Info.plist，不下载整包）
  - 按 bundleIdentifier 合并多版本，自动选最新版为当前版本
  - 本地 JSON 缓存，重复运行只解析新增文件
  - downloadURL 指向 AList 的 /d/<path>，每次请求由服务端重新签名

用法:
  python3 alist_source.py \
    --path "/苹果应用 - Appstore apps" \
    --path "/巨魔商店 - TrollStore" \
    --depth 2 --limit 300 --workers 6 \
    --out public/source.json --cache .cache/ipa.json

部署:
  把 public/ 整个目录托管到任意 HTTPS 静态站点（GitHub Pages / Cloudflare Pages），
  源地址填 https://<你的域名>/source.json
"""

import argparse
import concurrent.futures as futures
import json
import os
import plistlib
import re
import ssl
import struct
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import zlib

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
LOCK = threading.Lock()


def api(base, path, payload, timeout=25):
    req = urllib.request.Request(
        base.rstrip("/") + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": UA},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
        return json.loads(r.read().decode())


def http_range(url, start, end, timeout=40):
    req = urllib.request.Request(
        url, headers={"Range": f"bytes={start}-{end}", "User-Agent": UA}
    )
    with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
        if r.status not in (200, 206):
            raise RuntimeError(f"range failed: HTTP {r.status}")
        return r.read()


def read_at(url, offset, size, file_size, timeout=40):
    if size <= 0 or offset >= file_size:
        return b""
    end = min(offset + size - 1, file_size - 1)
    return http_range(url, offset, end, timeout)


def parse_ipa(url, file_size):
    tail_len = min(file_size, 65557)
    tail = read_at(url, file_size - tail_len, tail_len, file_size)
    idx = tail.rfind(b"PK\x05\x06")
    if idx < 0:
        raise RuntimeError("EOCD not found")
    eocd = tail[idx:idx + 22]
    cd_size, cd_offset = struct.unpack("<II", eocd[12:20])
    if cd_size == 0xFFFFFFFF or cd_offset == 0xFFFFFFFF:
        raise RuntimeError("ZIP64 not supported")

    cd = read_at(url, cd_offset, cd_size, file_size, timeout=90)
    pos, target = 0, None
    while pos + 46 <= len(cd):
        if cd[pos:pos + 4] != b"PK\x01\x02":
            break
        method, = struct.unpack("<H", cd[pos + 10:pos + 12])
        comp_size, _ = struct.unpack("<II", cd[pos + 20:pos + 28])
        name_len, extra_len, comment_len = struct.unpack("<HHH", cd[pos + 28:pos + 34])
        local_off, = struct.unpack("<I", cd[pos + 42:pos + 46])
        name = cd[pos + 46:pos + 46 + name_len].decode("utf-8", "replace")
        if name.startswith("Payload/") and name.endswith(".app/Info.plist"):
            target = (method, comp_size, local_off, name)
            break
        pos += 46 + name_len + extra_len + comment_len
    if not target:
        raise RuntimeError("Info.plist not found")

    method, comp_size, local_off, name = target
    lh = read_at(url, local_off, 30, file_size)
    if lh[:4] != b"PK\x03\x04":
        raise RuntimeError("bad local header")
    lname_len, lextra_len = struct.unpack("<HH", lh[26:30])
    data_start = local_off + 30 + lname_len + lextra_len
    raw = read_at(url, data_start, comp_size, file_size, timeout=90)
    if method == 8:
        raw = zlib.decompress(raw, -15)
    elif method != 0:
        raise RuntimeError(f"unsupported compression {method}")

    info = plistlib.loads(raw)
    return {
        "bundle_id": info.get("CFBundleIdentifier", ""),
        "version": info.get("CFBundleShortVersionString", "1.0"),
        "build": info.get("CFBundleVersion", "1"),
        "name": info.get("CFBundleDisplayName") or info.get("CFBundleName") or "",
    }


def collect(base, roots, depth, per_page=200):
    queue = [(r, 0) for r in roots]
    seen, files = set(), []
    while queue:
        path, d = queue.pop(0)
        if path in seen:
            continue
        seen.add(path)
        try:
            js = api(base, "/api/fs/list", {
                "path": path, "password": "", "page": 1,
                "per_page": per_page, "refresh": False,
            })
        except Exception as e:
            print(f"[warn] list {path}: {e}", file=sys.stderr)
            continue
        if js.get("code") != 200:
            print(f"[warn] list {path}: {js.get('message')}", file=sys.stderr)
            continue
        for it in (js.get("data") or {}).get("content") or []:
            full = it.get("virtual_path") or (path.rstrip("/") + "/" + it["name"])
            if it.get("is_dir"):
                if d < depth:
                    queue.append((full, d + 1))
            elif it["name"].lower().endswith(".ipa"):
                files.append({
                    "path": full,
                    "name": it["name"],
                    "size": it.get("size") or 0,
                    "modified": it.get("modified") or "",
                })
    return files


def version_key(v):
    return tuple(int(x) if x.isdigit() else 0 for x in re.split(r"[._-]", str(v)))


def resolve_one(base, f, cache):
    key = f["path"]
    with LOCK:
        if key in cache:
            return cache[key]
    try:
        js = api(base, "/api/fs/get", {"path": key, "password": ""})
        if js.get("code") != 200:
            raise RuntimeError(js.get("message") or "fs/get failed")
        raw_url = (js.get("data") or {}).get("raw_url") or ""
        if not raw_url:
            raise RuntimeError("no raw_url")
        meta = parse_ipa(raw_url, f["size"])
        if not meta["bundle_id"]:
            raise RuntimeError("empty bundle id")
        meta.update({"path": key, "size": f["size"], "modified": f["modified"],
                     "file_name": f["name"]})
        with LOCK:
            cache[key] = meta
        return meta
    except Exception as e:
        print(f"[fail] {f['name'][:60]}: {e}", file=sys.stderr)
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://pan.qlam.cc")
    ap.add_argument("--path", action="append", required=True,
                    help="可多次传入，如 --path /苹果应用... --path /巨魔商店...")
    ap.add_argument("--depth", type=int, default=1)
    ap.add_argument("--limit", type=int, default=200, help="最多解析多少个 IPA")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--out", default="public/source.json")
    ap.add_argument("--cache", default=".cache/ipa.json")
    ap.add_argument("--icon-base", default="",
                    help="图标 URL 前缀；留空使用站点 logo")
    ap.add_argument("--source-name", default="清烟镜像源")
    ap.add_argument("--source-id", default="cc.qlam.mirror")
    args = ap.parse_args()

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    os.makedirs(os.path.dirname(os.path.abspath(args.cache)), exist_ok=True)
    cache = {}
    if os.path.exists(args.cache):
        try:
            cache = json.load(open(args.cache, encoding="utf-8"))
        except Exception:
            cache = {}

    files = collect(args.base, args.path, args.depth)
    files.sort(key=lambda x: x["modified"], reverse=True)
    todo = files[:args.limit]
    print(f"[scan] {len(files)} ipa, parsing {len(todo)} (cache={len(cache)})")

    metas, done = [], 0
    with futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
        for meta in ex.map(lambda f: resolve_one(args.base, f, cache), todo):
            done += 1
            if meta:
                metas.append(meta)
            if done % 20 == 0:
                print(f"[parse] {done}/{len(todo)} ok={len(metas)}", flush=True)

    groups = {}
    for m in metas:
        groups.setdefault(m["bundle_id"], []).append(m)

    apps = []
    for bid, versions in groups.items():
        versions.sort(key=lambda m: (version_key(m["version"]), version_key(m["build"])), reverse=True)
        cur = versions[0]
        icon = (args.icon_base.rstrip("/") + "/" + bid + ".png") \
            if args.icon_base else "https://www.qlam.cc/img/favicon.png"
        apps.append({
            "name": cur["name"] or cur["file_name"].rsplit(".", 1)[0],
            "bundleIdentifier": bid,
            "developerName": "清烟资源库镜像",
            "subtitle": cur["file_name"],
            "localizedDescription": cur["file_name"],
            "iconURL": icon,
            "tintColor": "#6385ef",
            "versions": [{
                "version": m["version"],
                "buildVersion": m["build"],
                "date": (m["modified"] or "")[:10] or "2026-01-01",
                "downloadURL": args.base.rstrip("/") + "/d/" + urllib.parse.quote(m["path"], safe=""),
                "size": m["size"],
                "minOSVersion": "14.0",
            } for m in versions],
        })
    apps.sort(key=lambda a: a["versions"][0]["date"], reverse=True)

    source = {
        "name": args.source_name,
        "identifier": args.source_id,
        "subtitle": "基于 AList 公开接口自动生成",
        "description": "由 alist_source.py 生成，downloadURL 实时指向源站签名链接",
        "iconURL": "https://www.qlam.cc/img/favicon.png",
        "headerURL": "https://www.qlam.cc/img/uploads/logo.png",
        "tintColor": "#6385ef",
        "featuredApps": [a["bundleIdentifier"] for a in apps[:5]],
        "apps": apps,
        "news": [],
    }
    with open(args.out, "w", encoding="utf-8") as fp:
        json.dump(source, fp, ensure_ascii=False, indent=2)
    with open(args.cache, "w", encoding="utf-8") as fp:
        json.dump(cache, fp, ensure_ascii=False, indent=2)

    # 结构自检
    required = {"name", "bundleIdentifier", "versions"}
    for a in source["apps"]:
        if not required.issubset(a):
            raise SystemExit(f"invalid app entry: {a.get('bundleIdentifier')}")
        for v in a["versions"]:
            for k in ("version", "downloadURL", "size"):
                if not v.get(k):
                    raise SystemExit(f"invalid version entry: {a['bundleIdentifier']} {k}")

    print(f"[done] files={len(metas)} apps={len(apps)} -> {args.out}")


if __name__ == "__main__":
    main()
