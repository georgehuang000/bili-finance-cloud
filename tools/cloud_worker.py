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


def enqueue_history_recoveries():
    """Retry only missing archive scope once daily, after fresh data/report work."""
    now = int(time.time())
    queued = []
    for uid in db.subscribed_uids():
        history = crawler_runner.STATE["uppers"].get(uid, {}).get("history", {})
        if history.get("status") != "limited" or not history.get("video_missing"):
            continue
        if now - history.get("recovery_queued_ts", 0) < 86400:
            continue
        result = cloud.enqueue("crawl", uid, {"history_recovery": True})
        if result["ok"]:
            history["recovery_queued_ts"] = now
            queued.append(uid)
    if queued:
        crawler_runner._save_state()
    return queued


def execute(job):
    cloud.refresh_settings()
    if config.LLM_BASE_URL != config.CLOUD_LLM_BASES.get(config.LLM_PROVIDER):
        raise ValueError("云端分析服务地址不符合官方服务商配置")
    kind, uid, payload = job["kind"], job["uid"], job["payload"]
    if kind == "crawl" and uid and payload.get("history_recovery"):
        history = crawler_runner.STATE["uppers"].get(str(uid), {}).get("history", {})
        if history.get("status") != "limited":
            return []
        history["video_reference_total"] = max(history.get("video_reference_total", 0), history.get("video_total", 0))
        history.update(status="pending", videos_done=False, video_source=None,
                       video_pages=0, video_items=0, video_oid="")
        history.pop("error", None)
        crawler_runner._save_state()
    if kind in ("crawl", "scheduled"):
        if uid:
            result = crawler_runner.crawl_single(uid, full_history=bool(payload.get("full_history")))
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
            enqueue_history_recoveries()
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
    if kind in ("crawl", "scheduled"):
        return [author for author in ([str(uid)] if uid else db.subscribed_uids())
                if crawler_runner.STATE["uppers"].get(author, {}).get("history", {}).get("status") == "pending"]
    return []


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
            continuation = execute(job)
        except Exception as error:
            failures += 1
            # SDK/driver exception bodies can contain keys and connection URLs.
            message = str(error)[:250] if isinstance(error, ValueError) else "云端执行失败，请检查配置或网络"
            cloud.finish_job(job["id"], message, failed=True)
            print(f"job={job['id']} kind={job['kind']} failed ({type(error).__name__})")
        else:
            selected = [str(job["uid"])] if job["uid"] else db.subscribed_uids()
            limited = job["kind"] in ("crawl", "scheduled") and any(
                crawler_runner.STATE["uppers"].get(uid, {}).get("history", {}).get("status") == "limited"
                for uid in selected)
            message = "本批历史采集完成，继续补齐" if continuation else "可访问内容已采集，历史范围尚未核全" if limited else "执行完成"
            cloud.finish_job(job["id"], message)
            for uid in continuation:
                cloud.enqueue("crawl", uid)
            print(f"job={job['id']} kind={job['kind']} complete")
        finally:
            crawler_runner._save_state()
    db.engine.dispose()
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
