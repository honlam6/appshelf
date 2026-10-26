#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 AList 站点生成 AltStore / SideStore 软件源（source.json + 真实图标）。

特性:
  - 多目录扫描，递归深度可配
  - 并发远程解析 IPA（只读 EOCD/中央目录/Info.plist/AppIcon，不下载整包）
  - 提取 IPA 内 AppIcon，处理 iOS 的 CgBI PNG 并转标准 PNG
  - 按 bundleIdentifier 合并多版本，自动选最新版为当前版本
  - 本地 JSON 缓存 + 图标文件缓存，重复运行只处理新增

用法:
  export ALIST_BASE="https://your-alist-host"
  python3 alist_source.py \
    --path "/苹果应用 - Appstore apps" \
    --path "/巨魔商店 - TrollStore" \
    --depth 2 --limit 300 --workers 6 \
    --icon-base "https://appshelf.pages.dev" \
    --out source.json --cache .cache/ipa.json
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
DEFAULT_ICON = "https://github.com/honlam6.png"


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


def read_entry(url, file_size, entry, timeout=90):
    """读取 zip 中央目录条目的解压后内容。"""
    name, method, comp_size, _, local_off = entry
    lh = read_at(url, local_off, 30, file_size)
    if lh[:4] != b"PK\x03\x04":
        raise RuntimeError("bad local header")
    lname_len, lextra_len = struct.unpack("<HH", lh[26:30])
    data_start = local_off + 30 + lname_len + lextra_len
    raw = read_at(url, data_start, comp_size, file_size, timeout)
    if method == 8:
        raw = zlib.decompress(raw, -15)
    elif method != 0:
        raise RuntimeError(f"unsupported compression {method}")
    return raw


def _png_chunks(data):
    pos, ihdr, idat = 8, None, b""
    while pos + 8 <= len(data):
        ln = struct.unpack(">I", data[pos:pos + 4])[0]
        typ = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + ln]
        if typ == b"IHDR":
            ihdr = chunk
        elif typ == b"IDAT":
            idat += chunk
        elif typ == b"IEND":
            break
        pos += 12 + ln
    return ihdr, idat


def _encode_png(w, h, rgba):
    raw = b"".join(b"\x00" + bytes(rgba[y * w * 4:(y + 1) * w * 4]) for y in range(h))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + \
            struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    return b"\x89PNG\r\n\x1a\n" + \
        chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)) + \
        chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


def cgbi_to_png(data):
    """iOS AppIcon 常为 CgBI PNG（raw deflate + BGRA 预乘），转标准 RGBA PNG。"""
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    ihdr, idat = _png_chunks(data)
    if not ihdr or not idat:
        return None
    w, h, bd, ct, comp, filt, inter = struct.unpack(">IIBBBBB", ihdr)
    if bd != 8 or ct != 6 or inter != 0:
        return data  # 非 8-bit RGBA，原样返回
    try:
        zlib.decompress(idat)
        return data  # 已经是标准 PNG
    except zlib.error:
        pass
    try:
        raw = zlib.decompress(idat, -15)
    except zlib.error:
        return None
    stride = w * 4
    if len(raw) < h * (stride + 1):
        return None

    out = bytearray()
    prev = bytearray(stride)
    p = 0
    for _ in range(h):
        f = raw[p]
        p += 1
        line = bytearray(raw[p:p + stride])
        p += stride
        if f == 1:
            for i in range(4, stride):
                line[i] = (line[i] + line[i - 4]) & 255
        elif f == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 255
        elif f == 3:
            for i in range(stride):
                a = line[i - 4] if i >= 4 else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 255
        elif f == 4:
            for i in range(stride):
                a = line[i - 4] if i >= 4 else 0
                b = prev[i]
                c = prev[i - 4] if i >= 4 else 0
                pp = a + b - c
                pa, pb, pc = abs(pp - a), abs(pp - b), abs(pp - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 255
        out += line
        prev = line

    for i in range(0, len(out), 4):
        b, g, r, a = out[i], out[i + 1], out[i + 2], out[i + 3]
        if 0 < a < 255:
            r = min(255, r * 255 // a)
            g = min(255, g * 255 // a)
            b = min(255, b * 255 // a)
        out[i], out[i + 1], out[i + 2], out[i + 3] = r, g, b, a

    return _encode_png(w, h, out)


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
    entries, info_entry = [], None
    pos = 0
    while pos + 46 <= len(cd):
        if cd[pos:pos + 4] != b"PK\x01\x02":
            break
        method, = struct.unpack("<H", cd[pos + 10:pos + 12])
        comp_size, uncomp_size = struct.unpack("<II", cd[pos + 20:pos + 28])
        name_len, extra_len, comment_len = struct.unpack("<HHH", cd[pos + 28:pos + 34])
        local_off, = struct.unpack("<I", cd[pos + 42:pos + 46])
        name = cd[pos + 46:pos + 46 + name_len].decode("utf-8", "replace")
        entry = (name, method, comp_size, uncomp_size, local_off)
        entries.append(entry)
        if name.startswith("Payload/") and name.endswith(".app/Info.plist"):
            info_entry = entry
        pos += 46 + name_len + extra_len + comment_len

    if not info_entry:
        raise RuntimeError("Info.plist not found")

    info = plistlib.loads(read_entry(url, file_size, info_entry))

    icon_png = None
    candidates = [
        e for e in entries
        if e[0].startswith("Payload/") and ".app/" in e[0]
        and e[0].lower().endswith(".png")
        and ("appicon" in e[0].lower() or "/icon" in e[0].lower())
        and e[3] > 0
    ]
    candidates.sort(key=lambda e: e[3], reverse=True)
    for e in candidates[:4]:
        try:
            png = cgbi_to_png(read_entry(url, file_size, e))
            if png:
                icon_png = png
                break
        except Exception:
            continue

    return {
        "bundle_id": info.get("CFBundleIdentifier", ""),
        "version": info.get("CFBundleShortVersionString", "1.0"),
        "build": info.get("CFBundleVersion", "1"),
        "name": info.get("CFBundleDisplayName") or info.get("CFBundleName") or "",
        "icon_png": icon_png,
    }


def collect(base, roots, depth, per_page=200, workers=12):
    frontier = [(r, 0) for r in roots]
    seen, files = set(), []
    while frontier:
        batch = []
        while frontier and len(batch) < workers * 4:
            path, d = frontier.pop(0)
            if path in seen:
                continue
            seen.add(path)
            batch.append((path, d))
        if not batch:
            break

        def fetch(item):
            path, d = item
            try:
                js = api(base, "/api/fs/list", {
                    "path": path, "password": "", "page": 1,
                    "per_page": per_page, "refresh": False,
                })
            except Exception as e:
                js = {"code": 0, "message": str(e)}
            return item, js

        with futures.ThreadPoolExecutor(max_workers=workers) as ex:
            for (path, d), js in ex.map(fetch, batch):
                if js.get("code") != 200:
                    print(f"[warn] list {path}: {js.get('message')}", file=sys.stderr)
                    continue
                for it in (js.get("data") or {}).get("content") or []:
                    full = it.get("virtual_path") or (path.rstrip("/") + "/" + it["name"])
                    if it.get("is_dir"):
                        if d < depth:
                            frontier.append((full, d + 1))
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("ALIST_BASE", ""),
                    help="AList 站点根地址，也可用环境变量 ALIST_BASE")
    ap.add_argument("--path", action="append", required=True)
    ap.add_argument("--depth", type=int, default=1)
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--out", default="source.json")
    ap.add_argument("--cache", default=".cache/ipa.json")
    ap.add_argument("--icon-base", default="",
                    help="图标 URL 前缀，例如 https://appshelf.pages.dev")
    ap.add_argument("--source-name", default="App Shelf")
    ap.add_argument("--source-id", default="com.appshelf.source")
    args = ap.parse_args()

    if not args.base:
        ap.error("--base is required (or set ALIST_BASE)")
    out_dir = os.path.dirname(os.path.abspath(args.out))
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(os.path.dirname(os.path.abspath(args.cache)), exist_ok=True)
    icons_dir = os.path.join(out_dir, "icons")
    os.makedirs(icons_dir, exist_ok=True)

    cache = {}
    if os.path.exists(args.cache):
        try:
            cache = json.load(open(args.cache, encoding="utf-8"))
        except Exception:
            cache = {}

    files = collect(args.base, args.path, args.depth)
    files.sort(key=lambda x: x["modified"], reverse=True)
    todo = files[:args.limit]
    print(f"[scan] {len(files)} ipa, parsing {len(todo)} (cache={len(cache)})", flush=True)

    def resolve(f):
        key = f["path"]
        with LOCK:
            cached = cache.get(key)
        if cached:
            icon_path = os.path.join(icons_dir, cached["bundle_id"] + ".png")
            if os.path.exists(icon_path):
                return dict(cached, icon_png=None)
        try:
            js = api(args.base, "/api/fs/get", {"path": key, "password": ""})
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
                cache[key] = {k: v for k, v in meta.items() if k != "icon_png"}
            return meta
        except Exception as e:
            print(f"[fail] {f['name'][:60]}: {e}", file=sys.stderr)
            return None

    metas, done = [], 0
    with futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
        for meta in ex.map(resolve, todo):
            done += 1
            if meta:
                metas.append(meta)
            if done % 20 == 0:
                print(f"[parse] {done}/{len(todo)} ok={len(metas)}", flush=True)

    # 落盘图标
    for m in metas:
        if m.get("icon_png"):
            try:
                with open(os.path.join(icons_dir, m["bundle_id"] + ".png"), "wb") as fp:
                    fp.write(m["icon_png"])
            except Exception as e:
                print(f"[warn] icon write {m['bundle_id']}: {e}", file=sys.stderr)

    groups = {}
    for m in metas:
        groups.setdefault(m["bundle_id"], []).append(m)

    apps = []
    for bid, versions in groups.items():
        versions.sort(key=lambda m: (version_key(m["version"]), version_key(m["build"])), reverse=True)
        cur = versions[0]
        icon_path = os.path.join(icons_dir, bid + ".png")
        if args.icon_base and os.path.exists(icon_path):
            icon = args.icon_base.rstrip("/") + "/icons/" + urllib.parse.quote(bid) + ".png"
        else:
            icon = DEFAULT_ICON
        apps.append({
            "name": cur["name"] or cur["file_name"].rsplit(".", 1)[0],
            "bundleIdentifier": bid,
            "developerName": "App Shelf",
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
        "subtitle": "iOS / iPadOS 应用聚合",
        "description": "自动生成的 AltStore 软件源",
        "iconURL": DEFAULT_ICON,
        "headerURL": DEFAULT_ICON,
        "tintColor": "#6385ef",
        "featuredApps": [a["bundleIdentifier"] for a in apps[:5]],
        "apps": apps,
        "news": [],
    }
    with open(args.out, "w", encoding="utf-8") as fp:
        json.dump(source, fp, ensure_ascii=False, indent=2)
    with open(args.cache, "w", encoding="utf-8") as fp:
        json.dump(cache, fp, ensure_ascii=False, indent=2)

    print(f"[done] files={len(metas)} apps={len(apps)} icons={len(os.listdir(icons_dir))} -> {args.out}", flush=True)


if __name__ == "__main__":
    main()
