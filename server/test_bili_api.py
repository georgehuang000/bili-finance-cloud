# -*- coding: utf-8 -*-
"""快速测试 bili_api 模块（单 UID）。"""
import asyncio
import sys
sys.path.insert(0, ".")

import bili_api

async def main():
    uid = "525121722"
    print(f"Testing crawl_one_uid({uid}) ...")
    r = await bili_api.crawl_one_uid(uid)
    print(f"  ok: {r['ok']}")
    print(f"  videos: {r['video_count']}")
    print(f"  dynamics: {r['dynamic_count']}")
    print(f"  error: {r['error']}")
    print(f"  login_required: {r['login_required']}")
    if r["videos"]:
        print("  --- sample videos ---")
        for v in r["videos"][:3]:
            print(f"    {v['bvid']} play={v['play']} {v['title'][:35]}")
    if r["dynamics"]:
        print("  --- sample dynamics ---")
        for d in r["dynamics"][:3]:
            print(f"    {d['dyn_id']} type={d['type']} text={d['text'][:40]!r}")

asyncio.run(main())
