# -*- coding: utf-8 -*-
"""APScheduler wiring: crawl then summarize at 08:00 and 18:00 daily."""
import logging
import threading

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import config
import crawler_runner
import summarizer
import finance_analysis

log = logging.getLogger("scheduler")

_scheduler = None


def _job(period: str):
    log.info("scheduled job start: crawl -> summarize (%s)", period)
    try:
        if not crawler_runner.run_full_round_sync():
            log.warning("scheduled crawl skipped: a crawl is already running")
    except Exception:
        log.exception("scheduled crawl failed")
    try:
        if not summarizer.generate_sync(period=period):
            log.warning("scheduled summarize skipped: a summarization is already running")
    except Exception:
        log.exception("scheduled summarize failed")
    log.info("scheduled job done (%s)", period)


def _parse_hm(value: str, default_h: int, default_m: int):
    try:
        h, m = value.split(":")
        return int(h), int(m)
    except Exception:
        return default_h, default_m


def start():
    global _scheduler
    if config.CLOUD_MODE:
        return None
    if _scheduler is not None:
        return _scheduler
    _scheduler = BackgroundScheduler(timezone="Asia/Shanghai")

    mh, mm = _parse_hm(config.CRAWL_MORNING, 8, 0)
    eh, em = _parse_hm(config.CRAWL_EVENING, 18, 0)

    _scheduler.add_job(
        _job, CronTrigger(hour=mh, minute=mm), args=["morning"],
        id="morning", replace_existing=True,
    )
    _scheduler.add_job(
        _job, CronTrigger(hour=eh, minute=em), args=["evening"],
        id="evening", replace_existing=True,
    )
    _scheduler.start()
    if config.FINANCE_AUTO_ANALYSIS:
        _scheduler.add_job(finance_analysis.start_bg, 'interval', minutes=15,
                           id='finance_pending', replace_existing=True, max_instances=1)
        finance_analysis.start_bg(per_upper=2)
    log.info("scheduler started: morning=%02d:%02d evening=%02d:%02d", mh, mm, eh, em)

    if config.AUTO_CRAWL_ON_START:
        log.info("AUTO_CRAWL_ON_START enabled -> background round (crawl + summarize)")
        # full job in a thread so startup is never blocked; backfills a round
        # missed while the process/service was down
        threading.Thread(target=_job, args=["boot"], daemon=True).start()
    return _scheduler


def shutdown():
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
