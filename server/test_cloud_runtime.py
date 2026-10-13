"""Integration checks against an explicitly named, disposable local Postgres.

Set DATABASE_URL to the local bili_cloud_test fixture database. Never runs
destructive setup against Neon, the Pi database, or another project database.
No test makes an external Bilibili or model request.
"""
import json
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url
from cryptography.fernet import Fernet

import config
import db
import cloud_runtime as cloud
import finance_analysis as analysis
import finance_evidence as evidence
import main

_url = make_url(config.DATABASE_URL) if config.DATABASE_URL else None
_safe = bool(_url and _url.host in ("127.0.0.1", "localhost") and _url.database == "bili_cloud_test" and config.CLOUD_MODE)


@unittest.skipUnless(_safe, "Requires an explicitly named disposable local Postgres")
class CloudTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        config.ADMIN_PASSWORD_HASH = cloud.password_hash("fixture-password")
        config.CREDENTIAL_ENCRYPTION_KEY = Fernet.generate_key().decode()
        config.CLOUD_TEST_MODE = True
        db.init_db()
        analysis.init_store()
        evidence.init_store()

    def setUp(self):
        tables = inspect(db.engine).get_table_names()
        with db.engine.begin() as connection:
            connection.execute(text("TRUNCATE " + ",".join(f'"{name}"' for name in tables) + " RESTART IDENTITY"))
        db.add_upper("1000", "测试作者")
        self.client = TestClient(main.app, base_url="https://testserver")

    def owner(self):
        self.assertEqual(self.client.post("/api/auth/login", json={"password": "fixture-password"}).status_code, 200)

    def test_public_can_read_but_cannot_search_manage_or_run_models(self):
        self.assertEqual(self.client.get("/api/uppers").status_code, 200)
        self.assertEqual(self.client.get("/api/uppers/1000/videos").status_code, 200)
        self.assertEqual(self.client.get("/api/uppers/2000/videos").status_code, 404)
        for method, path in [("get", "/api/uppers/search?q=测试"), ("get", "/api/llm/settings"), ("get", "/api/login/qrcode/poll?key=12345678"), ("get", "/api/diagnostics"), ("post", "/api/uppers"), ("delete", "/api/uppers/1000"), ("post", "/api/crawl"), ("post", "/api/llm/test"), ("post", "/api/research/analyze"), ("post", "/api/research/evidence")]:
            self.assertEqual(getattr(self.client, method)(path).status_code, 401, path)

    def test_owner_session_secure_logout_and_origin(self):
        self.owner()
        self.assertTrue(self.client.get("/api/auth/session").json()["can_manage"])
        self.assertIn("HttpOnly", self.client.post("/api/auth/login", json={"password": "fixture-password"}).headers["set-cookie"])
        self.assertEqual(self.client.post("/api/crawl", headers={"origin": "https://evil.example"}).status_code, 403)
        self.assertEqual(self.client.post("/api/crawl", json={"uid": "1000"}).status_code, 200)
        self.client.post("/api/auth/logout")
        self.assertEqual(self.client.post("/api/crawl").status_code, 401)

    def test_persistent_login_rate_limit(self):
        for _ in range(10):
            self.assertEqual(self.client.post("/api/auth/login", json={"password": "wrong"}).status_code, 401)
        result = self.client.post("/api/auth/login", json={"password": "fixture-password"})
        self.assertIn("15分钟", result.json()["detail"])

    def test_credentials_are_encrypted_and_official_endpoint_enforced(self):
        cloud.save_settings({"LLM_API_KEY": "synthetic-test-secret", "LLM_BASE_URL": "https://api.deepseek.com/v1"})
        with db.session_scope() as session:
            self.assertNotIn("synthetic-test-secret", session.get(cloud.Setting, "LLM_API_KEY").encrypted)
        self.assertEqual(cloud.read_settings()["LLM_API_KEY"], "synthetic-test-secret")
        with self.assertRaises(ValueError):
            cloud.save_settings({"LLM_BASE_URL": "https://proxy.example"})

    def test_model_test_cannot_be_redirected_and_is_not_run_anonymously(self):
        self.owner()
        with patch("llm_client.test_connection", return_value={"ok": True}) as call:
            self.assertEqual(self.client.post("/api/llm/test", json={"base_url": "https://evil.example"}).status_code, 400)
            call.assert_not_called()
            self.assertEqual(self.client.post("/api/llm/test", json={"base_url": "https://api.deepseek.com/v1"}).status_code, 200)
            self.assertEqual(call.call_args.args[1], "https://api.deepseek.com/v1")

    def test_upserts_preserve_subtitle_and_image_recognition(self):
        db.upsert_videos([{"bvid": "av1", "uid": "1000", "title": "first", "subtitle": "retained", "image_desc": "retained"}])
        db.upsert_videos([{"bvid": "av1", "uid": "1000", "title": "second"}])
        with db.session_scope() as session:
            row = session.get(db.Video, "av1")
            self.assertEqual((row.title, row.subtitle, row.image_desc), ("second", "retained", "retained"))

    def test_finance_ids_provenance_and_transaction_rollback_on_postgres(self):
        sentence = "半导体设备订单继续增长，需求保持强劲。"
        source = {"kind": "video", "source_id": "av1", "pub_ts": 100, "title": "fixture", "url": "https://www.bilibili.com/video/av1", "text": sentence, "subtitle_segments": None}
        result = {"claims": [{"topic": "半导体", "subject": "半导体设备", "kind": "产业判断", "claim": "作者认为设备订单增长", "quote": sentence}]}
        with patch.object(analysis, "call_json", return_value=(result, "fixture-model")):
            analysis.analyze_source("1000", source)
        claim = analysis.list_claims("1000")[0]
        self.assertGreater(claim["id"], 0)
        self.assertEqual(analysis.get_claim(claim["id"])["quote"], sentence)
        with self.assertRaises(RuntimeError):
            with analysis.connect() as connection:
                connection.execute("INSERT INTO finance_attempts VALUES(?,?,?,?)", ("rollback", 1, "fixture", 1))
                raise RuntimeError("rollback")
        with analysis.connect() as connection:
            self.assertIsNone(connection.execute("SELECT * FROM finance_attempts WHERE input_hash=?", ("rollback",)).fetchone())

    def test_queue_deduplicates_parallel_calls_and_cold_start_preserves_running(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: cloud.enqueue("analyze", "1000", {"per_upper": 1}), range(4)))
        self.assertEqual(sum(r["ok"] for r in results), 1)
        job = cloud.take_job()
        self.assertEqual(job["uid"], "1000")
        self.assertIsNone(cloud.take_job())
        with analysis.connect() as connection:
            connection.execute("INSERT INTO finance_runs(uid,started_ts,status) VALUES(?,?,'running')", ("1000", 100))
        analysis.init_store()
        self.assertEqual(analysis.status()["last_run"]["status"], "running")
        cloud.finish_job(job["id"])
        self.assertEqual(cloud.jobs()[0]["status"], "complete")

    def test_cloud_test_requires_one_author_and_queues_one_item(self):
        self.owner()
        with patch("llm_client.is_configured", return_value=True):
            self.assertEqual(self.client.post("/api/research/analyze", json={}).status_code, 400)
            result = self.client.post("/api/research/analyze", json={"uid": "1000"})
            self.assertEqual(result.status_code, 200)
        job = cloud.take_job()
        self.assertEqual(job["payload"]["per_upper"], 1)


if __name__ == "__main__":
    unittest.main()
