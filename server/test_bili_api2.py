# -*- coding: utf-8 -*-
"""快速测试 bili_api 模块（单 UID）- 带详细 traceback。"""
import asyncio
import sys
import traceback
sys.path.insert(0, ".")

import bili_api

async def main():
    uid = "525121722"
    print(f"Testing fetch_videos({uid}) ...")
    try:
        videos = await bili_api.fetch_videos(uid)
        print(f"  OK: {len(videos)} videos")
        for v in videos[:2]:
            print(f"    {v['bvid']} {v['title'][:30]}")
    except Exception as e:
        traceback.print_exc()

    await asyncio.sleep(2)

    print(f"\nTesting fetch_dynamics({uid}) ...")
    try:
        dynamics = await bili_api.fetch_dynamics(uid)
        print(f"  OK: {len(dynamics)} dynamics")
        for d in dynamics[:2]:
            print(f"    {d['dyn_id']} {d['text'][:30]!r}")
    except Exception as e:
        traceback.print_exc()

asyncio.run(main())
