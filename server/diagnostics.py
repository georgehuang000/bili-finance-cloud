# -*- coding: utf-8 -*-
"""一站式诊断快照：出问题时先看这里，不用逐个接口猜。

设计原则：**只报告状态与计数，绝不输出任何凭据值**（Cookie/API key 仅报有无）。

覆盖排错最常见的几类疑问：
- 登录态是不是掉了？（login：当前状态 + 最近检测时间 + 缓存内容）
- .env 里哪些 Cookie 配了/没配？（env_cookies：布尔值）
- 是不是被风控？打在哪个接口、什么错误码、几次？（risk_stats）
- 爬取/总结在进行吗？冷却还剩多久？（crawl）
- 每个 UP 主最近一轮的结果？（uppers）
- 逐视频字幕/摘要为什么没生成？（enrich）
- 库里有数据吗？字幕/摘要覆盖率多少？（db）
"""
import sqlite3
import time

import config


def _db_stats() -> dict:
    """直接只读查库，避免与 ORM 会话相互影响。"""
    out = {}
    try:
        con = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True, timeout=5)
        try:
            cur = con.cursor()
            for table in ("uppers", "videos", "dynamics", "summaries"):
                out[table] = cur.execute(f"select count(*) from {table}").fetchone()[0]
            out["videos_with_subtitle"] = cur.execute(
                "select count(*) from videos where subtitle is not null and subtitle != ''"
            ).fetchone()[0]
            out["videos_with_summary"] = cur.execute(
                "select count(*) from videos where summary is not null and summary != ''"
            ).fetchone()[0]
        finally:
            con.close()
    except Exception as ex:
        out["error"] = str(ex)
    return out


def _env_cookie_presence() -> dict:
    """只报告是否已配置，不报告值。"""
    return {
        "BILI_SESSDATA": bool(config.BILI_SESSDATA),
        "BILI_JCT": bool(config.BILI_JCT),
        "BILI_BUVID3": bool(config.BILI_BUVID3),
        "BILI_BUVID4": bool(config.BILI_BUVID4),
        "BILI_DEDEUSERID": bool(config.BILI_DEDEUSERID),
    }


def _llm_snapshot() -> dict:
    """模型配置快照。只给掩码与来源键名，**绝不输出密钥原文**。"""
    import llm_settings

    cur = llm_settings.current()
    return {
        "configured": cur["configured"],
        "provider": cur["provider"],
        "base_url": cur["base_url"],
        "model": cur["model"],
        "vision_model": cur["vision_model"],
        "thinking": cur["thinking"],
        "vision_enabled": cur["vision_enabled"],
        "vision_max_images": cur["vision_max_images"],
        "key_masked": cur["key_masked"],
        "key_source": cur["key_source"],
        "hint": (
            "模型未配置：请在网页顶栏点「模型设置」填入 API Key"
            if not cur["configured"]
            else (
                "模型名可能不支持图片识别（DeepSeek 只有 deepseek-flash 支持）"
                if cur["vision_model"] and "pro" in cur["vision_model"]
                else "模型配置正常"
            )
        ),
    }


def snapshot() -> dict:
    import bili_api
    import crawler_runner
    import summarizer

    return {
        "now": int(time.time()),
        "login": {
            "login_required": bool(crawler_runner.STATE.get("login_required")),
            "last_checked_ts": crawler_runner.login_checked_ts(),
            "cached_check": bili_api.login_cache_snapshot(),
            "hint": (
                "登录态失效时 feed 会报 -352 而非 -101；请在网页点「扫码登录」"
                if crawler_runner.STATE.get("login_required")
                else "登录态正常"
            ),
        },
        "env_cookies": _env_cookie_presence(),
        "llm": _llm_snapshot(),
        "vision": {
            "enabled": bool(config.VISION_ENABLED),
            # 最近一轮图片识别的统计（封面识别了几张、失败几条、跳过原因）
            "last_pass": crawler_runner.STATE.get("vision") or {},
        },
        "crawl": {
            "crawling": crawler_runner.is_crawling(),
            "summarizing": summarizer.is_summarizing(),
            "use_pure_api": config.USE_PURE_API,
            "sleep_seconds": [config.CRAWL_SLEEP_MIN, config.CRAWL_SLEEP_MAX],
            "risk_cooldown_remaining": bili_api.risk_cooldown_remaining(),
            "risk_stats": bili_api.risk_stats(),
            "last_summary_error": crawler_runner.STATE.get("last_summary_error"),
        },
        "uppers": crawler_runner.get_status_uppers(),
        "enrich": crawler_runner.STATE.get("enrich", {}),
        "db": _db_stats(),
        "log_level": config.LOG_LEVEL,
    }