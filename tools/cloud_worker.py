"""Run durable jobs on GitHub Actions, never in a serverless background thread."""
import argparse
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

os.environ["CLOUD_MODE"] = "true"
os.environ["CLOUD_WORKER"] = "true"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

import config
import db
import cloud_runtime as cloud
import crawler_runner
import finance_analysis as analysis
import finance_evidence as evidence
import summarizer


def execute(job):
    cloud.refresh_settings()
    if urlsplit(config.LLM_BASE_URL).hostname != "api.deepseek.com":
        raise ValueError("云端仅允许 DeepSeek 官方 API")
    kind, uid, payload = job["kind"], job["uid"], job["payload"]
    if kind in ("crawl", "scheduled"):
        if uid:
            crawler_runner.crawl_single(uid)
        else:
            crawler_runner.run_full_round_sync()
        if kind == "scheduled":
            summarizer.generate_sync(payload.get("period", "cloud"))
        if not config.CLOUD_TEST_MODE and config.FINANCE_AUTO_ANALYSIS:
            analysis.run_batch(uid, per_upper=4)
    elif kind == "analyze":
        result = analysis.run_batch(uid, per_upper=1 if config.CLOUD_TEST_MODE else min(4, payload.get("per_upper", 4)))
        if result["failed"]:
            raise ValueError("模型分析失败，请检查 DeepSeek 设置或余额")
    elif kind == "summarize":
        summarizer.generate_sync(payload.get("period", "manual"))
    elif kind == "evidence":
        evidence.verify(payload["claim_id"], payload["url"])
    else:
        raise ValueError("不支持的云端任务")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scheduled", action="store_true")
    args = parser.parse_args()
    if not cloud.ready():
        print("DATABASE_URL 尚未配置；未运行任何收费任务")
        return
    db.init_db()
    analysis.init_store()
    evidence.init_store()
    if args.scheduled:
        if os.environ.get("CLOUD_SCHEDULE_ENABLED") != "true" or config.CLOUD_TEST_MODE:
            print("测试阶段定时采集关闭")
        else:
            cloud.enqueue("scheduled", payload={"period": "cloud"})
    failures = 0
    for _ in range(5):
        job = cloud.take_job()
        if not job:
            break
        try:
            execute(job)
        except Exception as error:
            failures += 1
            # SDK/driver exception bodies can contain keys and connection URLs.
            message = str(error)[:250] if isinstance(error, ValueError) else "云端执行失败，请检查配置或网络"
            cloud.finish_job(job["id"], message, failed=True)
            print(f"job={job['id']} kind={job['kind']} failed ({type(error).__name__})")
        else:
            cloud.finish_job(job["id"])
            print(f"job={job['id']} kind={job['kind']} complete")
    db.engine.dispose()
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
