# -*- coding: utf-8 -*-
"""视频字幕抓取 + 逐视频 AI 摘要。

链路（与 anti_spider/bili_api 共用 httpx client、Cookie 与 WBI 签名）：
  1. /x/web-interface/view?bvid=            -> cid（公开接口，无需 WBI）
  2. /x/player/wbi/v2?bvid=&cid= (WBI)      -> subtitle.subtitles[] 字幕轨列表
     AI 字幕(lan=ai-zh)一般需要登录态(SESSDATA)才下发；匿名时常见为空。
  3. GET subtitle_url (*.hdslb.com/*.json)  -> body[].content 逐句字幕，拼接成全文

enrich_recent_videos() 供爬取轮次调用：对近 N 小时内尚无摘要的视频
抓字幕（入库 videos.subtitle）并调 LLM 生成摘要（入库 videos.summary）。
单视频失败只记日志，不影响整轮爬取。
"""
import asyncio
import logging
import random
import time

import config
import db

logger = logging.getLogger("subtitle")

_MIN_VALID_SUBTITLE = 50      # 少于此长度视为无有效字幕


async def fetch_subtitle(video_ref: str) -> dict:
    """抓取单个视频的字幕全文 + view 元数据。失败时 text 为空字符串。

    video_ref: 库里的 bvid 字段，可能存的是 av 号（旧 MediaCrawler 约定）或真 BV 号。
    返回: {"text": str, "desc": str, "play": int, "pubdate": int}
    （desc/play/pubdate 来自 view 接口，用于回填 feed 拿不到的字段）
    """
    import bili_api

    meta = {"text": "", "desc": "", "play": 0, "pubdate": 0}

    if str(video_ref).lower().startswith("av"):
        view_params = {"aid": str(video_ref)[2:]}
    else:
        view_params = {"bvid": str(video_ref)}

    data = await bili_api._api_get(
        "https://api.bilibili.com/x/web-interface/view",
        view_params,
    )
    cid = data.get("cid")
    if not cid:
        pages = data.get("pages") or []
        cid = pages[0].get("cid") if pages else None
    if not cid:
        logger.warning(f"[subtitle] {video_ref}: no cid in view api")
        return meta
    # view 响应里带真正的 BV 号，后续接口统一用 BV
    bvid = data.get("bvid") or str(video_ref)
    meta["desc"] = data.get("desc") or ""
    meta["play"] = int((data.get("stat") or {}).get("view") or 0)
    meta["pubdate"] = int(data.get("pubdate") or 0)

    pdata = await bili_api._api_get(
        "https://api.bilibili.com/x/player/wbi/v2",
        {"bvid": bvid, "cid": cid},
        need_wbi=True,
    )
    tracks = (pdata.get("subtitle") or {}).get("subtitles") or []
    if not tracks:
        return meta

    # 优先 AI 中文字幕，其次任意中文轨，最后第一条
    track = (
        next((t for t in tracks if t.get("lan") == "ai-zh"), None)
        or next((t for t in tracks if str(t.get("lan", "")).startswith("zh")), None)
        or tracks[0]
    )
    url = track.get("subtitle_url") or ""
    if url.startswith("//"):
        url = "https:" + url
    if not url:
        return meta

    client = await bili_api._get_client()
    resp = await client.get(url)
    if resp.status_code != 200:
        logger.warning(f"[subtitle] {bvid}: download http {resp.status_code}")
        return meta
    try:
        lines = resp.json().get("body") or []
    except Exception:
        logger.warning(f"[subtitle] {bvid}: subtitle json parse failed")
        return meta

    text = "".join(
        ln.get("content", "") for ln in lines if isinstance(ln, dict)
    ).strip()
    logger.info(f"[subtitle] {bvid}: lan={track.get('lan')} lines={len(lines)} chars={len(text)}")
    meta["text"] = text[: config.SUBTITLE_MAX_CHARS]
    meta["segments"] = [
        {"text": ln.get("content", ""), "start": ln.get("from"), "end": ln.get("to")}
        for ln in lines if isinstance(ln, dict) and ln.get("content")
    ]
    return meta


async def enrich_recent_videos_async(uid: str, max_videos: int = 5, lookback_hours: int = 48) -> dict:
    """为单个 UID 近期新视频抓字幕并生成 AI 摘要（协程版）。

    只处理 summary 为空且 pub_ts 在回看窗口内的视频，天然幂等：
    已处理过的视频不会重复消耗 LLM 调用。

    必须在 bili_api 共享 httpx client 绑定的同一个 event loop 上运行
    （即与 crawl_one_uid 用同一个 loop）；独立进程/脚本场景用同步包装版。
    """
    import summarizer  # 函数内导入，避免 summarizer->crawler_runner 环

    since_ts = int(time.time()) - lookback_hours * 3600
    rows = db.recent_videos_unsummarized(str(uid), since_ts, limit=max_videos)
    out = {"uid": str(uid), "candidates": len(rows), "subtitled": 0, "summarized": 0, "errors": []}

    for r in rows:
        bvid = r["bvid"]
        try:
            res = await fetch_subtitle(bvid)
        except Exception as ex:
            res = {"text": "", "desc": "", "play": 0, "pubdate": 0}
            out["errors"].append(f"{bvid}:subtitle:{ex}")
            logger.warning(f"[subtitle] {bvid}: {ex}")

        text = res.get("text", "")
        if len(text.strip()) >= _MIN_VALID_SUBTITLE:
            db.update_video_subtitle(
                bvid, text,
                desc=res.get("desc"), play=res.get("play"), pubdate=res.get("pubdate"),
                segments=res.get("segments"),
            )
            out["subtitled"] += 1
        else:
            # 无字幕（AI字幕未覆盖）：只回填 view 元数据，不写空字幕
            if res.get("desc") or res.get("play") or res.get("pubdate"):
                db.update_video_subtitle(
                    bvid, "",
                    desc=res.get("desc"), play=res.get("play"), pubdate=res.get("pubdate"),
                )
            await asyncio.sleep(random.uniform(1, 2))
            continue

        try:
            summary = summarizer.summarize_video(
                r.get("title", ""), res.get("desc") or r.get("desc") or "", text
            )
        except Exception as ex:
            summary = None
            out["errors"].append(f"{bvid}:llm:{ex}")
            logger.warning(f"[subtitle] {bvid} llm: {ex}")
        if summary:
            db.update_video_summary(bvid, summary)
            out["summarized"] += 1

        await asyncio.sleep(random.uniform(2, 4))  # 视频间小间隔，降低风控压力

    return out


def enrich_recent_videos(uid: str, max_videos: int = 5, lookback_hours: int = 48) -> dict:
    """同步包装版：仅供独立脚本/进程调用（服务内请用 async 版，共享 client 的
    loop 边界限制见 enrich_recent_videos_async）。"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(
            enrich_recent_videos_async(uid, max_videos=max_videos, lookback_hours=lookback_hours)
        )
    finally:
        loop.close()
