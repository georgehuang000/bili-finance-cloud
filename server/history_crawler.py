"""Bounded history traversal with a checkpoint after each committed page.

The same worker mutex protects recent refreshes and backfills. A failed page
does not advance the cursor; retries upsert the same IDs without duplication.
No historical model calls or new credentials are introduced here.
"""
import asyncio
import random

import bili_api
import db

PAGES_PER_BATCH = 50


async def crawl(uid, progress, checkpoint, *, page_budget=PAGES_PER_BATCH):
    uid = str(uid)
    progress.setdefault("status", "pending")
    progress["status"] = "pending"
    progress.pop("error", None)
    checkpoint()
    videos = dynamics = 0
    seen_feed = {progress.get("feed_offset", "")}
    seen_video = {progress.get("video_oid", "")}
    try:
        for _ in range(page_budget):
            if not progress.get("feed_done") and progress.get("videos_done"):
                offset = progress.get("feed_offset", "")
                data = await bili_api.fetch_space_feed_page(uid, offset)
                if "has_more" not in data:
                    raise ValueError("history_feed_invalid_payload")
                items = data.get("items") or []
                more = bool(data.get("has_more"))
                next_offset = str(data.get("offset") or "")
                if more and (not items or not next_offset or next_offset in seen_feed):
                    raise ValueError("history_feed_no_progress")
                if not items and not offset and db.list_dynamics(uid, limit=1):
                    raise ValueError("empty_feed(soft_throttle)")
                dyn_rows, vid_rows = bili_api._parse_feed(items, uid)
                videos += db.upsert_videos(vid_rows)
                dynamics += db.upsert_dynamics(dyn_rows)
                progress.update(feed_done=not more, feed_offset=next_offset if more else "",
                                feed_pages=progress.get("feed_pages", 0) + 1)
                seen_feed.add(next_offset)
            elif not progress.get("videos_done"):
                page = await bili_api.fetch_archive_page(uid, progress.get("video_oid", ""))
                if page["has_more"] and (not page["rows"] or not page["next_oid"] or page["next_oid"] in seen_video):
                    raise ValueError("history_videos_no_progress")
                if not page["rows"] and page["total"]:
                    raise ValueError("history_videos_empty_payload")
                videos += db.upsert_videos(page["rows"])
                collected = progress.get("video_items", 0) + len(page["rows"])
                if not page["has_more"] and collected < page["total"]:
                    raise ValueError("history_videos_incomplete")
                progress.update(videos_done=not page["has_more"],
                                video_oid=page["next_oid"] if page["has_more"] else "",
                                video_pages=progress.get("video_pages", 0) + 1,
                                video_items=collected,
                                video_total=page["total"])
                seen_video.add(page["next_oid"])
            if progress.get("feed_done") and progress.get("videos_done"):
                progress["status"] = "complete"
            checkpoint()
            if progress["status"] == "complete":
                break
            await asyncio.sleep(random.uniform(1.5, 3.0))
    except Exception as error:
        # Network/SDK exception text may contain private request details.
        progress["status"] = "error"
        progress["error"] = str(error) if isinstance(error, ValueError) else type(error).__name__
        checkpoint()
        raise
    return {"ok": True, "count": videos + dynamics, "video_count": videos,
            "dynamic_count": dynamics, "error": None, "login_required": False,
            "history_pending": progress["status"] != "complete"}
