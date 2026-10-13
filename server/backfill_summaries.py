# -*- coding: utf-8 -*-
"""批量给历史视频补 AI 摘要。

只处理近 N 天的视频（更早的标题信息量够日报用，没必要花成本）。
每个视频先抓字幕再生成摘要，失败跳过不中断。

用法:
    .venv/Scripts/python.exe backfill_summaries.py [days]
    默认 days=7
"""
import asyncio
import logging
import sys
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

import db
import subtitle


async def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    week_ago = int(time.time()) - days * 86400

    rows = db.list_videos_missing_summary_since(week_ago)
    logger = logging.getLogger("backfill")
    logger.info(f"近 {days} 天缺摘要视频: {len(rows)} 条")

    success = 0
    for i, r in enumerate(rows):
        bvid = r["bvid"]
        logger.info(f"[{i+1}/{len(rows)}] {bvid}: {r['title'][:40]}")
        try:
            res = await subtitle.fetch_subtitle(bvid)
            text = res.get("text", "")
            if len(text.strip()) >= subtitle._MIN_VALID_SUBTITLE:
                db.update_video_subtitle(bvid, text,
                    desc=res.get("desc"), play=res.get("play"), pubdate=res.get("pubdate"))
            else:
                # 无字幕：只回填 view 元数据
                if res.get("desc") or res.get("play") or res.get("pubdate"):
                    db.update_video_subtitle(bvid, "",
                        desc=res.get("desc"), play=res.get("play"), pubdate=res.get("pubdate"))
                logger.info(f"  无字幕，跳过摘要")
                continue

            import summarizer
            summary = summarizer.summarize_video(r["title"], res.get("desc") or "", text)
            db.update_video_summary(bvid, summary)
            success += 1
            logger.info(f"  摘要 {len(summary)} 字")
        except Exception as ex:
            logger.warning(f"  失败: {ex}")
        await asyncio.sleep(2)

    logger.info(f"完成: {success}/{len(rows)} 成功")


if __name__ == "__main__":
    asyncio.run(main())
