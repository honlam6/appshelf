#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""枚举 iAPPOS 详情接口，导出应用元数据与直链。

接口: GET https://app.iappos.com/?detail=1&id=<id>
返回 JSON 中 bt1a 就是 IPA 直链（302 到对象存储），不校验会员/激活码。
前端只是把 dl/lg 写死为 false，用 bt2b 控制显示「解锁下载」。

用法:
  python3 iappos_grab.py --start 4032 --end 3900 --out iappos.json
  python3 iappos_grab.py --auto --max-miss 30 --out iappos.json
"""

import argparse
import concurrent.futures as futures
import json
import ssl
import sys
import urllib.request

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
BASE = "https://app.iappos.com"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"


def detail(i, timeout=15):
    req = urllib.request.Request(
        f"{BASE}/?detail=1&id={i}",
        headers={"User-Agent": UA, "Referer": BASE + "/"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
            d = json.loads(r.read().decode())
        if d.get("id") and d.get("name"):
            return {
                "id": d.get("id"),
                "name": d.get("name"),
                "version": d.get("nickname", ""),
                "icon": d.get("image", ""),
                "description": (d.get("keywords") or "").strip(),
                "download": d.get("bt1a", ""),
                "size": d.get("bt2a", ""),
                "free": d.get("bt2b") == "0",
                "updated": d.get("updatetime", ""),
            }
    except Exception:
        pass
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=int, default=4100)
    ap.add_argument("--end", type=int, default=3800)
    ap.add_argument("--auto", action="store_true", help="从 start 向下扫，连续 miss 达阈值停止")
    ap.add_argument("--max-miss", type=int, default=30)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--out", default="iappos.json")
    args = ap.parse_args()

    ids = list(range(args.start, args.end - 1, -1)) if not args.auto else []
    results = []

    if args.auto:
        miss = 0
        cur = args.start
        while miss < args.max_miss:
            batch = list(range(cur, cur - args.workers, -1))
            with futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
                got = list(ex.map(detail, batch))
            for d in got:
                if d:
                    results.append(d)
                    miss = 0
                else:
                    miss += 1
            cur -= args.workers
            print(f"[scan] cur={cur} found={len(results)} miss={miss}", flush=True)
    else:
        with futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
            for d in ex.map(detail, ids):
                if d:
                    results.append(d)
        print(f"[scan] ids={len(ids)} found={len(results)}")

    results.sort(key=lambda x: int(x["id"]), reverse=True)
    with open(args.out, "w", encoding="utf-8") as fp:
        json.dump(results, fp, ensure_ascii=False, indent=2)
    print(f"[done] {len(results)} apps -> {args.out}")


if __name__ == "__main__":
    main()
