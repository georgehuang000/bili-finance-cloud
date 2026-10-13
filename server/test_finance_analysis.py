"""Offline contract tests: provenance, history, isolation, retries and retrieval."""
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import config
import db
import finance_analysis as analysis
import finance_evidence as evidence
import main


TEXT = "半导体设备订单继续增长，需求保持强劲。"


def payload(quote=TEXT, change=None):
    return {"claims": [{"topic": "半导体", "subject": "半导体设备", "market": "A股", "kind": "产业判断", "stance": "看多", "horizon": "未明确", "claim": "作者认为设备需求保持强劲", "quote": quote, "rationale": [], "checks": ["核对公司订单披露"], "change": change}]}


class FinanceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "test.db"
        self.patches = [patch.object(config, "DB_PATH", self.path),
                        patch.object(db, "subscribed_uppers", return_value=[{"uid": "1000", "name": "作者甲"}, {"uid": "2000", "name": "作者乙"}]),
                        patch.object(db, "subscribed_uids", return_value=["1000", "2000"]),
                        patch.object(analysis.llm_client, "is_configured", return_value=True)]
        for item in self.patches:
            item.start()
        with analysis.connect() as c:
            c.executescript("CREATE TABLE uppers(uid TEXT PRIMARY KEY,name TEXT,subscribed INTEGER); INSERT INTO uppers VALUES('1000','作者甲',1),('2000','作者乙',1); CREATE TABLE videos(bvid TEXT,uid TEXT,title TEXT,url TEXT,pub_ts INTEGER,subtitle TEXT,subtitle_segments TEXT); CREATE TABLE dynamics(dyn_id TEXT,uid TEXT,text TEXT,url TEXT,pub_ts INTEGER);")
        analysis.init_store()
        evidence.init_store()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.directory.cleanup()

    def source(self, source_id="1", published=100, text=TEXT):
        return {"kind": "video", "source_id": source_id, "pub_ts": published,
                "title": "测试视频", "url": "https://www.bilibili.com/video/BVtest",
                "text": text, "subtitle_segments": None}

    def save(self, source=None, uid="1000", output=None):
        with patch.object(analysis, "call_json", return_value=(output or payload(), "test-model")):
            analysis.analyze_source(uid, source or self.source())

    def test_grounded_quote_and_no_hallucinated_source(self):
        self.save()
        row = analysis.list_claims()[0]
        self.assertEqual(row["quote"], TEXT)
        self.assertEqual(row["author"], "作者甲")
        self.assertIsNone(row["video_seconds"])
        with self.assertRaises(ValueError):
            self.save(self.source("2"), output=payload("模型编造了设备订单增长百分之三十"))
        self.assertEqual(len(analysis.list_claims()), 1)

    def test_incremental_cache_and_source_revision(self):
        source = self.source()
        self.save(source)
        self.assertTrue(analysis._done_or_cooling(analysis.fingerprint(source)))
        revised = self.source(text=TEXT + "后续仍需观察交付。")
        self.assertFalse(analysis._done_or_cooling(analysis.fingerprint(revised)))
        self.save(revised)
        self.assertEqual(len(analysis.list_claims()), 1)  # latest revision only

    def test_author_filter_and_append_only_history(self):
        self.save()
        previous_id = analysis.list_claims("1000")[0]["id"]
        self.save(self.source("2", 200), output=payload(change={"previous_id": previous_id, "kind": "新增论据", "summary": "新资料继续表达订单增长"}))
        self.save(self.source("3", 300), uid="2000")
        rows = analysis.list_claims("1000")
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["uid"] == "1000" for r in rows))
        self.assertEqual(rows[0]["previous"]["id"], previous_id)
        self.assertEqual(len(analysis.list_claims("2000")), 1)
        self.assertEqual(len(analysis.list_claims(topic="别的主题")), 0)

    def test_foreign_or_future_history_is_rejected(self):
        self.save(uid="2000")
        foreign = analysis.list_claims("2000")[0]["id"]
        with self.assertRaises(ValueError):
            self.save(self.source("2", 200), output=payload(change={"previous_id": foreign, "kind": "立场变化", "summary": "不可跨作者"}))

    def test_more_explicit_wording_is_not_a_stance_reversal(self):
        before = payload()
        before["claims"][0]["stance"] = "中性"
        self.save(output=before)
        old_id = analysis.list_claims()[0]["id"]
        self.save(self.source("2",200), output=payload(change={"previous_id":old_id,"kind":"立场变化","summary":"从中性到看多"}))
        self.assertEqual(analysis.list_claims()[0]["change"]["kind"], "表述变化")

    def test_whitespace_only_quote_matching_and_real_timecode(self):
        start, end = analysis.locate_quote("订单\n继续增长，需求强劲。", "订单继续增长，需求强劲。")
        self.assertEqual("订单\n继续增长，需求强劲。"[start:end], "订单\n继续增长，需求强劲。")
        source = self.source(text="开场白" + TEXT)
        source["subtitle_segments"] = json.dumps([{"text": "开场白", "start": 0}, {"text": TEXT, "start": 23.7}])
        self.assertEqual(analysis._timecode(source, 3), 23)

    def test_empty_finance_result_is_cached(self):
        with patch.object(analysis, "call_json", return_value=({"claims": []}, "test")):
            analysis.analyze_source("1000", self.source())
        self.assertTrue(analysis._done_or_cooling(analysis.fingerprint(self.source())))
        self.assertEqual(analysis.list_claims(), [])

    def test_failed_request_does_not_leak_sdk_error(self):
        with patch.object(analysis, "candidates", return_value=[self.source()]), patch.object(analysis, "call_json", side_effect=RuntimeError("secret_key_should_not_persist")):
            analysis.run_batch(uid="1000", per_upper=1)
        self.assertEqual(analysis.status()["last_run"]["failed"], 1)
        self.assertNotIn("secret_key", analysis.status()["errors"][0]["error"])
        self.assertTrue(analysis._done_or_cooling(analysis.fingerprint(self.source())))

    def test_unsafe_url_and_private_dns_are_blocked(self):
        for url in ["http://www.sec.gov/a", "https://127.0.0.1/a", "https://sec.gov.evil.test/a", "https://user:password@www.sec.gov/a"]:
            with self.assertRaises(ValueError):
                evidence.validate_url(url)
        with patch.object(evidence.socket, "getaddrinfo", return_value=[(None,None,None,None,("127.0.0.1",443))]):
            with self.assertRaises(ValueError):
                evidence.validate_url("https://www.sec.gov/a")

    def test_document_grounding_cache_and_missing_evidence(self):
        self.save()
        claim_id = analysis.list_claims()[0]["id"]
        body = "公司公告正文。" + TEXT + "报告期为2026年上半年。"
        result = {"status": "提供支持", "summary": "仅支持此公司订单增长", "quote": TEXT, "period": "2026年上半年", "limitations": "不证明行业整体表现"}
        with patch.object(evidence, "fetch_document", return_value=(body,"https://www.sec.gov/a","测试正文")), patch.object(analysis, "call_json", return_value=(result,config.LLM_MODEL)) as model:
            first = evidence.verify(claim_id,"https://www.sec.gov/a")
            self.assertEqual(first, evidence.verify(claim_id,"https://www.sec.gov/a"))
            self.assertEqual(model.call_count, 1)
        result["quote"] = ""
        with patch.object(evidence, "fetch_document", return_value=(body,"https://www.sec.gov/b","测试正文")), patch.object(analysis, "call_json", return_value=(result,"test")):
            with self.assertRaises(ValueError):
                evidence.verify(claim_id,"https://www.sec.gov/b")

    def test_api_selection_validation_and_empty_response(self):
        client = TestClient(main.app)  # no lifespan, scheduler, or model calls
        self.assertEqual(client.get("/api/research?uid=1000").json()["claims"], [])
        self.assertEqual(client.get("/api/research?uid=not-a-uid").status_code, 422)
        self.assertEqual(client.post("/api/research/analyze",json={"uid":"9999"}).status_code,404)


if __name__ == "__main__":
    unittest.main()
