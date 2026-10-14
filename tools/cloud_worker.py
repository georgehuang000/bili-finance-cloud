"""Run durable jobs on GitHub Actions, never in a serverless background thread."""
import argparse
import os
import sys
import time
from pathlib import Path

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
    if config.LLM_BASE_URL != config.CLOUD_LLM_BASES.get(config.LLM_PROVIDER):
        raise ValueError("云端分析服务地址不符合官方服务商配置")
    kind, uid, payload = job["kind"], job["uid"], job["payload"]
    if kind in ("crawl", "scheduled"):
        if uid:
            result = crawler_runner.crawl_single(uid)
            combined = result.get("detail", {}).get("combined", {})
            if not result.get("ok") or combined.get("error"):
                raise ValueError("B站采集未成功，请检查扫码登录状态或云端出口风控")
        else:
            if not crawler_runner.run_full_round_sync():
                raise ValueError("已有采集任务运行中，本轮未执行")
            if crawler_runner.STATE.get("login_required") or any(
                    crawler_runner.STATE["uppers"].get(author, {}).get("last_error")
                    for author in db.subscribed_uids()):
                raise ValueError("部分B站采集未成功，请检查登录和作者状态")
        if kind == "scheduled":
            if not summarizer.generate_sync(payload["period"], date=payload.get("date")):
                raise ValueError("已有日报任务运行中，本轮未生成")
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
    parser.add_argument("--period", choices=("morning", "evening"))
    parser.add_argument("--enqueue-only", action="store_true", help="Submit a scheduled report without executing queued jobs")
    args = parser.parse_args()
    if args.scheduled and not args.period:
        parser.error("--scheduled requires --period morning or evening")
    if args.enqueue_only and not args.scheduled:
        parser.error("--enqueue-only requires --scheduled")
    if not cloud.ready():
        print("DATABASE_URL 尚未配置；未运行任何收费任务")
        return
    state = cloud.load_state("crawl")
    for key in ("uppers", "enrich", "vision", "login_required", "last_summary_error"):
        if key in state:
            crawler_runner.STATE[key] = state[key]
    if args.scheduled:
        if os.environ.get("CLOUD_SCHEDULE_ENABLED") != "true" or config.CLOUD_TEST_MODE:
            print("测试阶段定时采集关闭")
        else:
            result = cloud.enqueue_scheduled(args.period)
            print(result["message"])
    if args.enqueue_only:
        db.engine.dispose()
        return
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
        finally:
            crawler_runner._save_state()
    db.engine.dispose()
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
