# -*- coding: utf-8 -*-
"""Crawl orchestration around MediaCrawler CLI.

Strategy (per task constraints):
- One UID per subprocess run so anonymized creator_hash/nickname can be
  attributed correctly.
- Two passes per full round: first CREATOR_MODE=True (videos) for every UID,
  then CREATOR_MODE=False (dynamics) for every UID. The mode is switched by
  rewriting external/MediaCrawler/config/bilibili_config.py.
- BILI_CREATOR_ID_LIST is rewritten to the single current UID.
- Output json for the run is moved to a temp dir named by UID before parsing.
- Random 5-10s sleep between UIDs; 412/risk-control failures are recorded as
  last_error and skipped (no infinite retry, round continues).
- Per-UID subprocess timeout: 5 minutes.
"""
import json
import logging
import random
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

import config
import db
import llm_client
import vision

logger = logging.getLogger("crawler")

# Pure API mode (no browser/subprocess)
if config.USE_PURE_API:
    import bili_api
    import asyncio
import dynamics_fetcher

# ---------------------------------------------------------------------------
# runtime state (in-memory + persisted to server/crawl_state.json)
# ---------------------------------------------------------------------------
_STATE_FILE = config.SERVER_DIR / "crawl_state.json"
_lock = threading.Lock()

STATE = {
    "crawling": False,
    "summarizing": False,
    "login_required": False,
    "last_summary_error": None,
    # uid -> {"last_crawl_ts": int|None, "last_error": str|None}
    "uppers": {},
    # uid -> 最近一轮逐视频 enrich 统计（字幕/摘要成败，排错用）
    "enrich": {},
    # 最近一轮图片识别统计（封面/配图各识别了几张、失败原因）
    "vision": {},
}

# 最近一次登录态检测时间（unix 秒），0=从未检测
_login_checked_ts = 0
# 后台登录态复查是否正在跑（避免并发重复检测）
_login_check_running = [False]


def _load_state():
    global _login_checked_ts
    if config.CLOUD_MODE:
        return
    if _STATE_FILE.exists():
        try:
            data = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
            STATE["uppers"] = data.get("uppers", {})
            STATE["login_required"] = data.get("login_required", False)
            STATE["enrich"] = data.get("enrich", {})
            STATE["vision"] = data.get("vision", {})
            _login_checked_ts = int(data.get("login_checked_ts") or 0)
        except Exception:
            pass
    for uid in db.subscribed_uids():
        STATE["uppers"].setdefault(str(uid), {"last_crawl_ts": None, "last_error": None})


def _save_state():
    if config.CLOUD_MODE:
        from cloud_runtime import save_state
        save_state("crawl", {**STATE, "login_checked_ts": _login_checked_ts})
        return
    try:
        _STATE_FILE.write_text(
            json.dumps(
                {
                    "uppers": STATE["uppers"],
                    "login_required": STATE["login_required"],
                    "login_checked_ts": _login_checked_ts,
                    "enrich": STATE["enrich"],
                    "vision": STATE["vision"],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception:
        pass


def _set_upper_state(uid, last_crawl_ts=None, last_error=..., login_required=None):
    uid = str(uid)
    st = STATE["uppers"].setdefault(uid, {"last_crawl_ts": None, "last_error": None})
    if last_crawl_ts is not None:
        st["last_crawl_ts"] = last_crawl_ts
    if last_error is not ...:
        st["last_error"] = last_error
    if login_required is not None:
        STATE["login_required"] = login_required
    _save_state()


def get_status_uppers():
    out = []
    for u in db.subscribed_uppers():
        uid = u["uid"]
        st = STATE["uppers"].get(uid, {})
        out.append(
            {
                "uid": uid,
                "name": u["name"],
                "last_crawl_ts": st.get("last_crawl_ts"),
                "last_error": st.get("last_error"),
            }
        )
    return out


def last_crawl_ts(uid):
    return STATE["uppers"].get(str(uid), {}).get("last_crawl_ts")


def last_error(uid):
    """该 UID 最近一轮的错误标签（None=正常）。供前端展示抓取健康度。"""
    return STATE["uppers"].get(str(uid), {}).get("last_error")


_load_state()


# ---------------------------------------------------------------------------
# config file rewriting
# ---------------------------------------------------------------------------
def _set_mc_config(creator_mode: bool, uid: str):
    text = config.MC_CONFIG_FILE.read_text(encoding="utf-8")
    # CREATOR_MODE
    text, n_mode = re.subn(
        r"^CREATOR_MODE\s*=\s*(True|False)",
        f"CREATOR_MODE = {creator_mode}",
        text,
        count=1,
        flags=re.MULTILINE,
    )
    # BILI_CREATOR_ID_LIST -> single uid
    text, n_list = re.subn(
        r"BILI_CREATOR_ID_LIST\s*=\s*\[.*?\]",
        f'BILI_CREATOR_ID_LIST = [\n    "{uid}",\n]',
        text,
        count=1,
        flags=re.DOTALL,
    )
    if n_mode != 1 or n_list != 1:
        raise RuntimeError(
            f"_set_mc_config: rewrite failed (CREATOR_MODE matched {n_mode}, "
            f"BILI_CREATOR_ID_LIST matched {n_list}) in {config.MC_CONFIG_FILE}"
        )
    config.MC_CONFIG_FILE.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# output file handling
# ---------------------------------------------------------------------------
def _cleanup_tmp(max_days: int = 7):
    """Delete json files older than max_days from tmp/ and tmp/backup/
    so disk usage stays bounded."""
    cutoff = time.time() - max_days * 86400
    for d in (config.TMP_DIR, config.TMP_DIR / "backup"):
        if not d.exists():
            continue
        for f in d.glob("*.json"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except Exception:
                pass


def _clear_outputs(prefix: str):
    """Move away any existing json files matching prefix so the run's output
    can be unambiguously attributed to the current UID."""
    config.MC_JSON_DIR.mkdir(parents=True, exist_ok=True)
    config.TMP_DIR.mkdir(parents=True, exist_ok=True)
    backup = config.TMP_DIR / "backup"
    backup.mkdir(exist_ok=True)
    for f in config.MC_JSON_DIR.glob(f"{prefix}*.json"):
        try:
            shutil.move(str(f), str(backup / f"{int(time.time()*1000)}_{f.name}"))
        except Exception:
            try:
                f.unlink()
            except Exception:
                pass


def _collect_output(prefix: str, uid: str, kind: str):
    """Return path to the newest json produced by the run, moved to tmp."""
    files = sorted(
        config.MC_JSON_DIR.glob(f"{prefix}*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not files:
        return None
    src = files[0]
    dst = config.TMP_DIR / f"{uid}_{kind}.json"
    try:
        shutil.move(str(src), str(dst))
    except Exception:
        return src
    return dst


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------
def _to_int(v):
    try:
        return int(str(v).strip())
    except Exception:
        return 0


def _parse_videos(path: Path, uid: str):
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = []
    for it in data:
        aid = str(it.get("video_id", "")).strip()
        if not aid:
            continue
        bvid = aid if aid.startswith("av") else f"av{aid}"
        desc = it.get("desc", "")
        if desc == "-":
            desc = ""
        url = it.get("video_url") or f"https://www.bilibili.com/video/{bvid}"
        rows.append(
            {
                "bvid": bvid,
                "uid": str(uid),
                "title": it.get("title", ""),
                "cover": it.get("video_cover_url", ""),
                "desc": desc,
                "pub_ts": _to_int(it.get("create_time")),
                "play": _to_int(it.get("video_play_count")),
                "url": url,
            }
        )
    return rows


def _parse_dynamics(path: Path, uid: str):
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = []
    for it in data:
        dyn_id = str(it.get("dynamic_id", "")).strip()
        if not dyn_id:
            continue
        rows.append(
            {
                "dyn_id": dyn_id,
                "uid": str(uid),
                "type": it.get("type", ""),
                "text": it.get("text", ""),
                "pub_ts": _to_int(it.get("pub_ts")),
                "url": f"https://t.bilibili.com/{dyn_id}",
            }
        )
    return rows


# ---------------------------------------------------------------------------
# subprocess run
# ---------------------------------------------------------------------------
_LOGIN_FAIL_PAT = re.compile(r"(二维码|扫码|scan the qrcode|login.*fail|登录失效|未登录)", re.I)
_RISK_PAT = re.compile(r"(412|风控|precondition failed|-352|-401)", re.I)


def _run_one(uid: str, creator_mode: bool):
    """Run a single MediaCrawler subprocess for one uid and one mode.

    Returns dict: {ok, count, error, login_required, output}
    """
    kind = "contents" if creator_mode else "dynamics"
    prefix = "creator_contents_" if creator_mode else "creator_dynamics_"

    _set_mc_config(creator_mode, uid)
    _clear_outputs(prefix)

    cmd = [
        str(config.MC_PYTHON),
        "main.py",
        "--platform", "bili",
        "--type", "creator",
        "--lt", "qrcode",
        "--save_data_option", "json",
        "--get_comment", "no",
        "--creator_id", str(uid),
    ]
    combined = ""
    login_required = False
    error = None
    # Popen + communicate so we can kill the whole process tree on timeout;
    # on Windows terminating the parent does not cascade to Chrome children.
    proc = subprocess.Popen(
        cmd,
        cwd=str(config.MEDIACRAWLER_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        stdout, stderr = proc.communicate(timeout=config.CRAWL_TIMEOUT_PER_UID)
        combined = (stdout or "") + "\n" + (stderr or "")
        rc = proc.returncode
    except subprocess.TimeoutExpired as e:
        # kill the entire process tree (MediaCrawler + Chrome children)
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
            capture_output=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=15)
            combined = (stdout or "") + "\n" + (stderr or "")
        except Exception:
            combined = (e.stdout or "") if isinstance(e.stdout, str) else ""
        rc = -1
        error = "timeout(5min)"

    tail = combined[-4000:]
    if _LOGIN_FAIL_PAT.search(tail):
        login_required = True
        error = error or "login_required"
    if _RISK_PAT.search(tail):
        error = error or "risk_control(412)"

    # parse output regardless (partial data may exist)
    out_path = _collect_output(prefix, uid, kind)
    count = 0
    if out_path is not None:
        try:
            if creator_mode:
                rows = _parse_videos(out_path, uid)
                count = db.upsert_videos(rows)
            else:
                rows = _parse_dynamics(out_path, uid)
                count = db.upsert_dynamics(rows)
        except Exception as ex:  # parsing failure
            error = error or f"parse_error:{ex}"

    ok = count > 0 or (error is None and rc == 0)
    if error is None and count == 0 and not ok:
        error = "no_data"
    return {
        "ok": ok,
        "count": count,
        "error": error,
        "login_required": login_required,
    }


def _run_dynamics(uid: str, max_retries: int = 1):
    """Fetch dynamics for one uid via the standalone Playwright fetcher.

    Bypasses MediaCrawler's httpx-based feed/space call (which is
    deterministically 412'd by Bilibili) in favour of an in-page fetch that
    reuses the persisted login profile. On a -352/412 risk-control hit it backs
    off 60-120s and retries once. Returns the same dict shape as _run_one:
    {ok, count, error, login_required}.
    """
    attempt = 0
    while True:
        try:
            res = dynamics_fetcher.fetch_dynamics(str(uid), max_items=30, pages=3)
        except Exception as ex:
            return {"ok": False, "count": 0, "error": f"exception:{ex}",
                    "login_required": False}

        count = 0
        error = res.get("error")
        items = res.get("items") or []
        if items:
            try:
                count = db.upsert_dynamics(items)
            except Exception as ex:
                error = error or f"parse_error:{ex}"

        # detect login/risk-control situations from the fetcher result
        login_required = False
        code = res.get("code")
        http_status = res.get("http_status")
        risk = False
        if count == 0:
            if code in (-352, -401, -509) or http_status == 412:
                risk = True
                error = error or f"risk_control(code={code},http={http_status})"
            elif code == -101 or http_status in (401, 403):
                login_required = True
                error = error or "login_required"

        # back off and retry once on risk control
        if risk and attempt < max_retries:
            attempt += 1
            time.sleep(random.uniform(60, 120))
            continue

        ok = count > 0
        if not ok and error is None:
            error = "no_data"
        return {
            "ok": ok,
            "count": count,
            "error": error,
            "login_required": login_required,
        }


def _sleep_between():
    time.sleep(random.uniform(config.CRAWL_SLEEP_MIN, config.CRAWL_SLEEP_MAX))


def _check_login_state() -> bool:
    """每轮开始前确认 B站登录态，返回 True 表示可以继续爬取。

    SESSDATA 失效后 feed 会以 -352（风控）而不是 -101（未登录）表现，不主动
    检测就会把「未登录」误判成「风控」并徒劳重试；而匿名请求风控阈值极低，
    继续爬只会加重限流并拖长本轮，故未登录时直接跳过本轮。
    检测不出来（网络异常等）时按可爬处理，避免误跳过整轮。
    """
    import asyncio

    try:
        loop = asyncio.new_event_loop()
        try:
            logged_in = loop.run_until_complete(bili_api.check_login())
        finally:
            loop.close()
    except Exception as ex:
        logger.warning("[crawler] login precheck error: %s", ex)
        return True

    if logged_in is None:
        return True

    _record_login_state(logged_in)
    if not logged_in:
        return False
    return True


def _record_login_state(logged_in: bool, source: str = ""):
    """记录登录态检测结果（供 /api/status 与 /api/diagnostics 展示）。"""
    global _login_checked_ts
    _login_checked_ts = int(time.time())
    if STATE["login_required"] != (not logged_in):
        logger.warning(
            "[crawler] 登录态变化：%s%s",
            "已失效" if not logged_in else "恢复正常",
            f"（{source}）" if source else "",
        )
    STATE["login_required"] = not logged_in
    _save_state()
    if not logged_in:
        logger.error(
            "[crawler] B站登录态已失效（nav isLogin=false）：feed 会以 -352 风控表现，"
            "本轮跳过。请在网页点「扫码登录」，或按 README 更新 .env 中的 "
            "BILI_SESSDATA / BILI_JCT 等 Cookie。"
        )


def login_checked_ts():
    """最近一次登录态检测时间（unix 秒，0=从未检测）。"""
    return _login_checked_ts


def maybe_refresh_login_state(ttl: int = 300):
    """登录态缓存过期时，在后台线程复查一次（不阻塞调用方）。

    这样网页 30s 轮询 /api/status 时，登录态失效最多 5 分钟内就会显示出来，
    不必等到下一轮爬取才发现。
    """
    import asyncio

    if _login_check_running[0] or STATE["crawling"]:
        return
    if time.time() - _login_checked_ts < ttl:
        return

    _login_check_running[0] = True

    def _worker():
        try:
            loop = asyncio.new_event_loop()
            try:
                logged_in = loop.run_until_complete(bili_api.check_login())
            finally:
                loop.close()
            if logged_in is not None:
                _record_login_state(logged_in, source="后台复查")
        except Exception as ex:
            logger.warning("[crawler] login recheck failed: %s", ex)
        finally:
            _login_check_running[0] = False

    threading.Thread(target=_worker, daemon=True).start()


def _sleep_between_dynamics():
    """Larger, more human-like spacing between UIDs for the dynamics pass to
    avoid the -352 rate trip observed on back-to-back feed/space calls."""
    time.sleep(random.uniform(config.DYN_SLEEP_MIN, config.DYN_SLEEP_MAX))


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------
def is_crawling():
    return STATE["crawling"]


def _try_begin_crawl() -> bool:
    """Atomically claim the crawl mutex. Returns False if already crawling."""
    with _lock:
        if STATE["crawling"]:
            return False
        STATE["crawling"] = True
        return True


def _end_crawl():
    with _lock:
        STATE["crawling"] = False
    if config.CLOUD_MODE:
        _save_state()
        return
    # Every entry point (scheduled/manual/single author) feeds incremental analysis.
    # The periodic pending job retries work skipped while another analysis runs.
    try:
        import finance_analysis
        if config.FINANCE_AUTO_ANALYSIS:
            finance_analysis.start_bg()
    except Exception as ex:
        logger.warning("finance analysis trigger failed: %s", type(ex).__name__)


def _run_pure_api(uid: str, full_history: bool = False) -> dict:
    """Run pure API crawl for one UID (videos + dynamics in one call).

    Returns dict compatible with the old _run_one shape:
    {ok, count, error, login_required}
    """
    loop = asyncio.new_event_loop()
    video_count = 0
    dyn_count = 0
    try:
        author = STATE["uppers"].setdefault(str(uid), {})
        history = author.get("history")
        needs_history = (full_history or (history and history.get("status") not in ("complete", "limited"))
                         or (history is None and not bili_api._has_history(str(uid))))
        if needs_history:
            import history_crawler
            if history is None or history.get("status") in ("complete", "limited"):
                author["history"] = {}
            result = loop.run_until_complete(history_crawler.crawl(
                str(uid), author["history"], _save_state))
            return result
        result = loop.run_until_complete(bili_api.crawl_one_uid(str(uid)))

        # Upsert into db
        video_count = 0
        dyn_count = 0
        if result["videos"]:
            try:
                video_count = db.upsert_videos(result["videos"])
            except Exception as ex:
                if not result["error"]:
                    result["error"] = f"db_error:{ex}"
        if result["dynamics"]:
            try:
                dyn_count = db.upsert_dynamics(result["dynamics"])
            except Exception as ex:
                if not result["error"]:
                    result["error"] = f"db_error:{ex}"

        # 字幕抓取 + 逐视频 AI 摘要：必须在同一个 event loop 上跑
        # （bili_api 共享 httpx client 绑定在该 loop），失败不影响本轮
        try:
            import subtitle
            enriched = loop.run_until_complete(
                subtitle.enrich_recent_videos_async(str(uid), max_videos=5)
            )
            result["enriched"] = enriched
            STATE["enrich"][str(uid)] = {
                "ts": int(time.time()),
                "candidates": enriched.get("candidates", 0),
                "subtitled": enriched.get("subtitled", 0),
                "summarized": enriched.get("summarized", 0),
                "errors": enriched.get("errors", [])[:5],
            }
            _save_state()
            logger.info(
                "[crawler] uid=%s enrich: candidates=%s subtitled=%s summarized=%s",
                uid, enriched["candidates"], enriched["subtitled"], enriched["summarized"],
            )
            # 候选>0 但一条字幕都没拿到，最常见原因是登录态失效（AI 字幕需登录才下发）
            if enriched.get("candidates") and not enriched.get("subtitled"):
                logger.warning(
                    "[crawler] uid=%s enrich: %s 个候选视频均未取到字幕——"
                    "常见原因是登录态失效（AI 字幕需登录），或该批视频无 AI 字幕",
                    uid, enriched["candidates"],
                )
            for err in enriched.get("errors", []):
                logger.warning("[crawler] uid=%s enrich error: %s", uid, err)
        except Exception as ex:
            logger.warning("[crawler] uid=%s enrich failed: %s", uid, ex)
    finally:
        # 共享 httpx client 绑定在本 loop 上，跨 loop 复用会报
        # "Event loop is closed"；先在本 loop 内正常关闭，下一个 UID 重建
        try:
            loop.run_until_complete(bili_api.aclose_client())
        except Exception:
            pass
        loop.close()

    return {
        "ok": result["ok"],
        "count": video_count + dyn_count,
        "error": result["error"],
        "login_required": result["login_required"],
        "video_count": video_count,
        "dynamic_count": dyn_count,
        "enriched": result.get("enriched"),
    }


def crawl_single(uid: str, do_videos: bool = True, do_dynamics: bool = True, full_history: bool = False):
    """Crawl a single UID (used for verification / targeted refresh)."""
    if not _try_begin_crawl():
        return {"ok": False, "message": "already crawling"}
    try:
        # ── Pure API mode ──
        if config.USE_PURE_API:
            # 订阅新 UP 时也会走这里：登录态失效就先跳过，避免匿名硬打接口
            if not _check_login_state():
                # 记下原因，好让卡片上显示「登录态失效」而不是一片空白
                _set_upper_state(uid, last_error="login_required")
                return {"ok": False, "message": "login_required"}
            r = _run_pure_api(uid, full_history=full_history)
            _set_upper_state(
                uid,
                last_crawl_ts=int(time.time()),
                last_error=r.get("error"),
                login_required=bool(r.get("login_required")),
            )
            return {"ok": True, "detail": {"uid": str(uid), "combined": r}}

        # ── Legacy MediaCrawler mode ──
        summary = {"uid": str(uid)}
        err = None
        login_required = False
        if do_videos:
            r = _run_one(uid, True)
            summary["videos"] = r
            if r["error"]:
                err = r["error"]
            login_required = login_required or bool(r.get("login_required"))
        if do_dynamics:
            _sleep_between()
            r = _run_dynamics(uid)
            summary["dynamics"] = r
            if r["error"] and not err:
                err = r["error"]
            login_required = login_required or bool(r.get("login_required"))
        _set_upper_state(
            uid,
            last_crawl_ts=int(time.time()),
            last_error=err,
            login_required=login_required,
        )
        return {"ok": True, "detail": summary}
    finally:
        _end_crawl()


def _run_vision_pass() -> dict:
    """对最近新增内容做一轮图片识别（视频封面为主，动态配图有才做）。

    为什么放在整轮末尾跑一次、而不是每个 UID 跑一次：
    配额（VISION_MAX_IMAGES_PER_ROUND）是按轮算的，且这一步是串行的模型调用，
    逐 UID 跑会把配额摊薄、也让单 UID 的耗时不可控。

    实测背景：B站 feed 里动态配图绝大多数为空（充电专属会被剥离成
    MAJOR_TYPE_BLOCKED，纯文字贴本来就没图），所以稳定可用的输入是视频封面。

    失败不抛异常：识别失败会写入带前缀的占位文本，避免每轮反复重试同一张坏图。
    """
    stats = {
        "ts": int(time.time()),
        "skipped": "",
        "covers": 0,
        "covers_failed": 0,
        "dynamics": 0,
        "dynamics_failed": 0,
        "images": 0,
        "errors": [],
    }

    def _finish():
        STATE["vision"] = stats
        _save_state()
        return stats

    if not config.VISION_ENABLED:
        stats["skipped"] = "disabled"
        return _finish()
    if not llm_client.is_configured():
        stats["skipped"] = "no_llm_key"
        return _finish()
    # 当前服务商/模型不支持读图就别白跑（不支持读图的网关传图会直接 400）
    import llm_settings

    if llm_settings.vision_capable() is False:
        stats["skipped"] = "provider_no_vision"
        return _finish()

    budget = max(0, int(config.VISION_MAX_IMAGES_PER_ROUND or 0))
    if budget <= 0:
        stats["skipped"] = "budget_zero"
        return _finish()

    since = int(time.time()) - max(1, int(config.VISION_LOOKBACK_DAYS)) * 86400

    # ── 1) 视频封面 ──
    try:
        rows = db.videos_missing_image_desc(since, limit=budget)
    except Exception as ex:
        logger.warning("[vision] 查询待识别视频失败: %s", ex)
        rows = []
    for row in rows:
        if budget <= 0:
            break
        try:
            desc = vision.describe([row["cover"]], kind="cover", context=row["title"])
            db.set_video_image_desc(row["bvid"], desc)
            stats["covers"] += 1
            stats["images"] += 1
        except Exception as ex:
            db.set_video_image_desc(row["bvid"], f"{db.IMAGE_FAIL_PREFIX} {ex}")
            stats["covers_failed"] += 1
            stats["errors"].append(f"封面 {row['bvid']}: {ex}")
        budget -= 1

    # ── 2) 动态配图（有配图才做）──
    if budget > 0:
        try:
            drows = db.dynamics_missing_image_desc(since, limit=budget)
        except Exception as ex:
            logger.warning("[vision] 查询待识别动态失败: %s", ex)
            drows = []
        for row in drows:
            if budget <= 0:
                break
            pics = row.get("pics") or []
            if not pics:
                continue
            use = pics[: min(len(pics), vision.MAX_PICS_PER_ITEM, budget)]
            try:
                desc = vision.describe(use, kind="dynamic", context=row.get("text", ""))
                db.set_dynamic_image_desc(row["dyn_id"], desc)
                stats["dynamics"] += 1
                stats["images"] += len(use)
                budget -= len(use)
            except Exception as ex:
                db.set_dynamic_image_desc(row["dyn_id"], f"{db.IMAGE_FAIL_PREFIX} {ex}")
                stats["dynamics_failed"] += 1
                stats["errors"].append(f"配图 {row['dyn_id']}: {ex}")
                budget -= len(use)

    stats["errors"] = stats["errors"][:5]
    if stats["images"] or stats["covers_failed"] or stats["dynamics_failed"]:
        logger.info(
            "[vision] 封面识别 %s 张（失败 %s）、动态配图 %s 条（失败 %s）",
            stats["covers"], stats["covers_failed"], stats["dynamics"], stats["dynamics_failed"],
        )
    return _finish()


def run_full_round():
    """Full round: videos + dynamics for all UIDs.

    NOTE: does NOT manage the crawling mutex itself; callers must go through
    start_full_round_bg() or run_full_round_sync().
    """
    # ── Pure API mode: one pass per UID (videos+dynamics together) ──
    if config.USE_PURE_API:
        if not _check_login_state():
            return {"ok": False, "skipped": "login_required", "detail": {}}
        result = {}
        for uid in db.subscribed_uids():
            try:
                r = _run_pure_api(uid)
            except Exception as ex:
                r = {"ok": False, "count": 0, "error": f"exception:{ex}",
                     "login_required": False}
            result[str(uid)] = r
            _set_upper_state(
                uid,
                last_crawl_ts=int(time.time()),
                last_error=r.get("error"),
                login_required=bool(r.get("login_required")),
            )
            _sleep_between()
        # 图片识别：整轮末尾跑一次（配额按轮算；失败不影响爬取结果）
        try:
            _run_vision_pass()
        except Exception as ex:
            logger.warning("[vision] pass failed: %s", ex)
        return {"ok": True, "detail": result}

    # ── Legacy MediaCrawler mode ──
    result = {"videos": {}, "dynamics": {}}
    _cleanup_tmp(max_days=7)
    # pass 1: videos
    for uid in db.subscribed_uids():
        try:
            r = _run_one(uid, True)
        except Exception as ex:
            r = {"ok": False, "count": 0, "error": f"exception:{ex}",
                 "login_required": False}
        result["videos"][str(uid)] = r
        _set_upper_state(
            uid,
            last_crawl_ts=int(time.time()),
            last_error=r.get("error"),
            login_required=bool(r.get("login_required")),
        )
        _sleep_between()
    # pass 2: dynamics (standalone Playwright fetcher, bypasses MediaCrawler)
    for uid in db.subscribed_uids():
        try:
            r = _run_dynamics(uid)
        except Exception as ex:
            r = {"ok": False, "count": 0, "error": f"exception:{ex}",
                 "login_required": False}
        result["dynamics"][str(uid)] = r
        # merge error info without clobbering last_crawl_ts
        prev = STATE["uppers"].get(str(uid), {}).get("last_error")
        merged = r.get("error") or prev
        _set_upper_state(
            uid,
            last_crawl_ts=int(time.time()),
            last_error=merged,
            login_required=bool(r.get("login_required")),
        )
        _sleep_between_dynamics()
    try:
        _run_vision_pass()
    except Exception as ex:
        logger.warning("[vision] pass failed: %s", ex)
    return {"ok": True, "detail": result}


def run_full_round_sync() -> bool:
    """Guarded synchronous full round (used by the scheduler).
    Returns False without doing anything if a crawl is already running."""
    if not _try_begin_crawl():
        return False
    try:
        run_full_round()
    finally:
        _end_crawl()
    return True


def start_full_round_bg() -> bool:
    """Claim the crawl mutex and start a background full round.
    Returns False (nothing started) if a crawl is already running."""
    if not _try_begin_crawl():
        return False

    def _worker():
        try:
            run_full_round()
        finally:
            _end_crawl()

    threading.Thread(target=_worker, daemon=True).start()
    return True


def start_single_bg(uid: str, full_history: bool = False) -> bool:
    """Start a background crawl of a single UID (e.g. right after subscribing).

    crawl_single 自带互斥申请；正在全量爬取时返回 False（本轮跳过，
    新 UID 会由下一个定时轮次覆盖）。
    """
    if is_crawling():
        return False

    def _worker():
        try:
            crawl_single(str(uid), full_history=full_history)
        except Exception as ex:
            logger.error("[crawler] single crawl uid=%s failed: %s", uid, ex)

    threading.Thread(target=_worker, daemon=True).start()
    return True
