"""Offline regression tests; no Bilibili requests, model calls or live writes."""
import asyncio
import copy
import unittest
from unittest.mock import AsyncMock, patch

import bili_api
import history_crawler as history
import crawler_runner as runner


def feed(identity, more=False, offset=""):
    return {"items": [{"id_str": identity, "type": "DYNAMIC_TYPE_WORD",
                       "modules": {"module_author": {"pub_ts": 100},
                                   "module_dynamic": {"desc": {"text": identity}}}}],
            "has_more": more, "offset": offset}


def archive(identity, more=False, total=1):
    return {"rows": [{"bvid": identity, "uid": "1000"}], "has_more": more,
            "next_oid": identity, "total": total}


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.progress = {}
        self.saved = []
        self.dynamic_ids = set()
        self.video_ids = set()
        def upsert_dyn(rows):
            self.dynamic_ids.update(row["dyn_id"] for row in rows)
            return len(rows)
        def upsert_vid(rows):
            self.video_ids.update(row["bvid"] for row in rows)
            return len(rows)
        self.patches = [patch.object(history.db, "upsert_dynamics", side_effect=upsert_dyn),
                        patch.object(history.db, "upsert_videos", side_effect=upsert_vid),
                        patch.object(bili_api, "_has_history", return_value=False),
                        patch.object(history.asyncio, "sleep", new=AsyncMock())]
        for item in self.patches:
            item.start()
    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
    def checkpoint(self):
        self.saved.append(copy.deepcopy(self.progress))
    def run_history(self, budget=50):
        return asyncio.run(history.crawl("1000", self.progress, self.checkpoint, page_budget=budget))

    def test_history_passes_recent_page_limits_and_finishes_both_sources(self):
        with patch.object(bili_api, "fetch_space_feed_page", new=AsyncMock(side_effect=[
                feed("d1", True, "a"), feed("d2", True, "b"), feed("d3")])), \
             patch.object(bili_api, "fetch_archive_page", new=AsyncMock(side_effect=[
                archive("v1", True, 3), archive("v2", True, 3), archive("v3", total=3)])):
            result = self.run_history()
        self.assertFalse(result["history_pending"])
        self.assertEqual(self.dynamic_ids, {"d1", "d2", "d3"})
        self.assertEqual(self.video_ids, {"v1", "v2", "v3"})
        self.assertEqual(self.saved[-1]["status"], "complete")

    def test_batch_boundary_resumes_at_committed_cursor(self):
        self.progress["videos_done"] = True
        with patch.object(bili_api, "fetch_space_feed_page", new=AsyncMock(side_effect=[
                feed("d1", True, "a"), feed("d2", True, "b")])):
            self.assertTrue(self.run_history(budget=2)["history_pending"])
        self.assertEqual(self.saved[-1]["feed_offset"], "b")
        with patch.object(bili_api, "fetch_space_feed_page", new=AsyncMock(return_value=feed("d3"))) as fetch, \
             patch.object(bili_api, "fetch_archive_page", new=AsyncMock(return_value=archive("v1"))):
            self.run_history()
        fetch.assert_awaited_once_with("1000", "b")
        self.assertEqual(self.progress["status"], "complete")

    def test_failed_page_keeps_progress_and_retry_does_not_skip_it(self):
        self.progress["videos_done"] = True
        with patch.object(bili_api, "fetch_space_feed_page", new=AsyncMock(side_effect=[
                feed("d1", True, "a"), RuntimeError("synthetic-private-request-detail")])):
            with self.assertRaises(RuntimeError):
                self.run_history()
        self.assertEqual(self.saved[-1]["feed_offset"], "a")
        self.assertEqual(self.saved[-1]["error"], "RuntimeError")
        with patch.object(bili_api, "fetch_space_feed_page", new=AsyncMock(return_value=feed("d2"))) as fetch, \
             patch.object(bili_api, "fetch_archive_page", new=AsyncMock(return_value=archive("v1"))):
            self.run_history()
        fetch.assert_awaited_once_with("1000", "a")
        self.assertEqual(self.dynamic_ids, {"d1", "d2"})

    def test_repeated_offset_does_not_falsely_finish(self):
        self.progress["videos_done"] = True
        with patch.object(bili_api, "fetch_space_feed_page", new=AsyncMock(side_effect=[
                feed("d1", True, "a"), feed("d2", True, "a")])):
            with self.assertRaisesRegex(ValueError, "no_progress"):
                self.run_history()
        self.assertEqual(self.progress["status"], "error")
        self.assertFalse(self.progress.get("feed_done"))
        self.assertEqual(self.dynamic_ids, {"d1"})

    def test_empty_video_payload_with_existing_total_is_not_complete(self):
        self.progress.update(feed_done=True)
        with patch.object(bili_api, "fetch_archive_page", new=AsyncMock(return_value={
                "rows": [], "has_more": False, "next_oid": "", "total": 401})):
            with self.assertRaisesRegex(ValueError, "empty_payload"):
                self.run_history()
        self.assertEqual(self.progress["status"], "error")
        self.assertFalse(self.progress.get("videos_done"))

    def test_collaboration_requires_verified_membership(self):
        item = {"id": 123, "bv_id": "BVfixture", "upper": {"mid": "2000"}}
        async def request(url, params, **kwargs):
            if url == bili_api.ARCHIVES_URL:
                self.assertEqual(params["biz_id"], "1000")
                self.assertEqual(params["with_current"], 0)
                return {"media_list": [item], "total_count": 1, "has_more": False}
            return {"owner": {"mid": "2000"}, "staff": [{"mid": "1000"}]}
        with patch.object(bili_api, "_api_get", new=AsyncMock(side_effect=request)):
            page = asyncio.run(bili_api.fetch_archive_page("1000"))
        self.assertEqual(page["rows"][0]["uid"], "1000")
        with patch.object(bili_api, "_api_get", new=AsyncMock(side_effect=[
                {"media_list": [item], "total_count": 1, "has_more": False},
                {"owner": {"mid": "2000"}, "staff": []}])):
            with self.assertRaisesRegex(ValueError, "author_mismatch"):
                asyncio.run(bili_api.fetch_archive_page("1000"))

    def test_premature_video_end_does_not_claim_full_coverage(self):
        self.progress.update(feed_done=True)
        with patch.object(bili_api, "fetch_archive_page", new=AsyncMock(return_value=archive("v1", total=401))):
            result = self.run_history()
        self.assertEqual(self.progress["status"], "limited")
        self.assertEqual(self.progress["video_missing"], 400)
        self.assertFalse(result["history_pending"])

    def test_verified_video_end_with_missing_items_does_not_block_dynamics(self):
        self.progress.update(video_pages=14, video_items=398, video_oid="last")
        with patch.object(bili_api, "fetch_archive_page", new=AsyncMock(return_value={
                "rows": [], "total": 401, "has_more": False, "next_oid": ""})), \
             patch.object(bili_api, "fetch_space_feed_page", new=AsyncMock(return_value=feed("d-final"))):
            self.run_history()
        self.assertEqual(self.dynamic_ids, {"d-final"})
        self.assertEqual(self.progress["status"], "limited")
        self.assertEqual(self.progress["video_missing"], 3)
        self.assertTrue(self.progress["feed_done"])

    def test_new_author_or_interrupted_backfill_never_uses_recent_refresh(self):
        for prior, populated in [(None, False), ({"status": "error", "feed_offset": "a"}, True)]:
            author = {} if prior is None else {"history": prior}
            with patch.dict(runner.STATE, {"uppers": {"1000": author}}), \
                 patch.object(bili_api, "_has_history", return_value=populated), \
                 patch.object(runner, "_save_state"), \
                 patch.object(bili_api, "aclose_client", new=AsyncMock()), \
                 patch.object(history, "crawl", new=AsyncMock(return_value={"ok": True})) as backfill, \
                 patch.object(bili_api, "crawl_one_uid", new=AsyncMock()) as recent:
                runner._run_pure_api("1000")
                backfill.assert_awaited_once()
                recent.assert_not_awaited()

    def test_completed_author_uses_incremental_path(self):
        with patch.dict(runner.STATE, {"uppers": {"1000": {"history": {"status": "complete"}}}}), \
             patch.object(history, "crawl", new=AsyncMock()) as backfill, \
             patch.object(bili_api, "aclose_client", new=AsyncMock()), \
             patch.object(bili_api, "crawl_one_uid", new=AsyncMock(return_value={
                 "ok": True, "videos": [], "dynamics": [], "error": None, "login_required": False})) as recent, \
             patch("subtitle.enrich_recent_videos_async", new=AsyncMock(side_effect=RuntimeError("offline"))):
            runner._run_pure_api("1000")
        recent.assert_awaited_once_with("1000")
        backfill.assert_not_awaited()

    def test_limited_history_does_not_restart_the_entire_backfill_each_round(self):
        with patch.dict(runner.STATE, {"uppers": {"1000": {"history": {"status": "limited", "video_missing": 3}}}}), \
             patch.object(history, "crawl", new=AsyncMock()) as backfill, \
             patch.object(bili_api, "aclose_client", new=AsyncMock()), \
             patch.object(bili_api, "crawl_one_uid", new=AsyncMock(return_value={
                 "ok": True, "videos": [], "dynamics": [], "error": None, "login_required": False})) as recent, \
             patch("subtitle.enrich_recent_videos_async", new=AsyncMock(side_effect=RuntimeError("offline"))):
            runner._run_pure_api("1000")
        recent.assert_awaited_once_with("1000")
        backfill.assert_not_awaited()

    def test_recent_search_uses_subscribed_name_for_new_author(self):
        with patch.object(bili_api, "fetch_space_feed", new=AsyncMock(return_value=feed("d1")["items"])), \
             patch.object(bili_api.db, "subscribed_uppers", return_value=[{"uid": "1000", "name": "new-author-name"}]), \
             patch.object(bili_api, "fetch_videos_by_search", new=AsyncMock(return_value=[])) as search:
            asyncio.run(bili_api.crawl_one_uid("1000"))
        search.assert_awaited_once_with("1000", "new-author-name")

    def test_creator_archive_is_preferred_and_uses_explicit_page(self):
        payload = {"list": {"vlist": [{"aid": 123, "mid": "1000", "bvid": "BVfixture", "created": 100}]},
                   "page": {"count": 51}, "is_risk": False}
        with patch.object(bili_api, "_api_get", new=AsyncMock(return_value=payload)) as request:
            page = asyncio.run(bili_api.fetch_archive_page("1000", source=None))
        self.assertEqual(request.call_args.args[0], bili_api.CREATOR_ARCHIVES_URL)
        self.assertEqual(request.call_args.args[1]["pn"], 1)
        self.assertEqual(page["source"], "legacy")
        self.assertTrue(page["has_more"])
        self.assertEqual(page["rows"][0]["bvid"], "av123")

    def test_blocked_creator_list_can_choose_fallback_only_on_first_page(self):
        with patch.object(bili_api, "_api_get", new=AsyncMock(side_effect=[
                bili_api.BiliApiError(-403, "fixture"),
                {"media_list": [], "has_more": False, "total_count": 0}])) as request:
            page = asyncio.run(bili_api.fetch_archive_page("1000", source=None))
        self.assertEqual(page["source"], "medialist")
        self.assertEqual(request.await_count, 2)
        with patch.object(bili_api, "_api_get", new=AsyncMock(side_effect=bili_api.BiliApiError(-403, "fixture"))) as request:
            with self.assertRaises(bili_api.BiliApiError):
                asyncio.run(bili_api.fetch_archive_page("1000", page=2, source="legacy"))
        self.assertEqual(request.await_count, 1)

    def test_unknown_archive_source_is_rejected_before_any_request(self):
        with patch.object(bili_api, "_api_get", new=AsyncMock()) as request:
            with self.assertRaisesRegex(ValueError, "unknown_source"):
                asyncio.run(bili_api.fetch_archive_page("1000", source="https://untrusted.example"))
        request.assert_not_awaited()

    def test_local_request_during_active_crawl_cannot_reset_its_checkpoint(self):
        import main
        progress = {"status": "pending", "feed_offset": "already-committed"}
        with patch.object(main.config, "CLOUD_MODE", False), \
             patch.dict(runner.STATE, {"crawling": True, "uppers": {"1000": {"history": progress}}}):
            result = main.post_crawl(main.CrawlReq(uid="1000", full_history=True))
            self.assertFalse(result["ok"])
            self.assertEqual(runner.STATE["uppers"]["1000"]["history"], progress)

    def test_video_only_author_can_have_no_dynamics(self):
        with patch.object(bili_api, "fetch_archive_page", new=AsyncMock(return_value=archive("v1"))), \
             patch.object(bili_api, "fetch_space_feed_page", new=AsyncMock(return_value={"items": [], "has_more": False})), \
             patch.object(history.db, "list_dynamics", return_value=[]):
            self.assertFalse(self.run_history()["history_pending"])
        self.assertEqual(self.video_ids, {"v1"})
        self.assertEqual(self.progress["status"], "complete")


if __name__ == "__main__":
    unittest.main()
