# -*- coding: utf-8 -*-
"""端到端验证字幕链路：抓取真实视频字幕 -> LLM 生成摘要 -> 打印。

用法:
    .venv/Scripts/python.exe test_subtitle.py [bvid]
不传 bvid 时默认用库里最新的一条视频。
"""
import asyncio
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

import db
import subtitle


def main():
    db.init_db()  # 执行建表 + subtitle/summary 列迁移
    bvid = sys.argv[1] if len(sys.argv) > 1 else None
    if not bvid:
        row = db.list_videos("322005137", limit=1)
        if not row:
            print("库里没有视频，请传入 bvid，例如: test_subtitle.py av116996670750822")
            return
        bvid = row[0]["bvid"]
    print(f"=== 测试视频: {bvid} ===")

    res = asyncio.run(subtitle.fetch_subtitle(bvid))
    text = res.get("text", "")
    print(f"\n[1] 字幕抓取: {len(text)} 字符 (desc={len(res.get('desc',''))}字 play={res.get('play')})")
    if text:
        print(f"    开头: {text[:120]}")

    if len(text.strip()) < subtitle._MIN_VALID_SUBTITLE:
        print("\n[!] 未拿到有效字幕。常见原因：")
        print("    - 匿名请求（无 BILI_SESSDATA）拿不到 AI 字幕轨 —— 需在 .env 配置 Cookie")
        print("    - 该视频本身没有 AI 字幕/CC 字幕")
        return

    row = db.list_videos("322005137", limit=200)
    meta = next((v for v in row if v["bvid"] == bvid), None)
    title = meta["title"] if meta else bvid
    desc = meta["desc"] if meta else ""

    import summarizer
    summary = summarizer.summarize_video(title, desc, text)
    print(f"\n[2] LLM 摘要 ({len(summary)} 字):\n{summary}")


if __name__ == "__main__":
    main()
