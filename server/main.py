# -*- coding: utf-8 -*-
"""FastAPI application exposing the monitoring API.

Run:  cd server; ./.venv/Scripts/python.exe -m uvicorn main:app --port 9000
"""
import asyncio
import logging
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from pydantic import BaseModel, Field

import config
import db
import bili_api
import bili_login
import crawler_runner
import diagnostics
import llm_client
import summarizer
import scheduler
import finance_analysis
import finance_evidence
import cloud_runtime as cloud
import cloud_api


def _setup_logging():
    """让应用日志（爬取/enrich 轨迹）真正输出到 stdout。

    此前没人调用 basicConfig，root logger 停在 WARNING，导致
    `logger.info("[crawler] uid=... enrich: ...")` 这类排查关键信息
    在服务里完全看不到（journalctl 里只有 warning）。
    """
    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # httpx/httpcore 每个请求都打 INFO，太吵
    for noisy in ("httpx", "httpcore", "openai", "sqlalchemy.engine"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


_setup_logging()
logger = logging.getLogger("main")

# UP 主 UID：3 位以上纯数字（B站 UID 实际均 ≥ 6 位，放宽以兼容历史号）
_UID_RE = re.compile(r"^\d{3,}$")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not config.CLOUD_MODE:
        db.init_db()
        finance_analysis.init_store()
        finance_evidence.init_store()
    elif cloud.ready() and config.CREDENTIAL_ENCRYPTION_KEY:
        cloud.refresh_settings()
    scheduler.start()
    yield
    scheduler.shutdown()


app = FastAPI(title="B站UP主投资内容监控", lifespan=lifespan,
              docs_url=None if config.CLOUD_MODE else "/docs",
              redoc_url=None if config.CLOUD_MODE else "/redoc")
cloud_api.configure(app)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, error: RequestValidationError):
    if config.CLOUD_MODE:
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": "请求参数格式不正确"}, status_code=422)
    return await request_validation_exception_handler(request, error)

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/uppers")
def get_uppers(request: Request):
    rows = db.list_uppers()
    if config.CLOUD_MODE:
        state = cloud.load_state("crawl")
        for row in rows:
            value = state.get("uppers", {}).get(row["uid"], {})
            row["last_crawl_ts"] = value.get("last_crawl_ts")
            row["last_error"] = value.get("last_error") if request.state.owner else None
            row["history_status"] = value.get("history", {}).get("status")
            row["history_video_total"] = value.get("history", {}).get("video_total")
        return rows
    for r in rows:
        r["last_crawl_ts"] = crawler_runner.last_crawl_ts(r["uid"])
        r["last_error"] = crawler_runner.last_error(r["uid"])
        r["history_status"] = crawler_runner.STATE["uppers"].get(r["uid"], {}).get("history", {}).get("status")
    return rows


# --- 订阅管理：搜索 / 添加 / 取消订阅 ---

class AddUpperReq(BaseModel):
    uid: str
    name: str = ""  # 可选，仅当 B站信息拉取失败时兜底展示


@app.get("/api/uppers/search")
def search_uppers(q: str = Query(..., min_length=1, max_length=50)):
    """订阅搜索：纯数字输入按 UID 直查用户卡片，否则按昵称关键词搜索。

    返回 {candidates: [{uid, name, face, fans, sign?, videos?}]}。
    """
    q = q.strip()
    if not q:
        raise HTTPException(status_code=400, detail="搜索词不能为空")
    try:
        if _UID_RE.match(q):
            try:
                card = asyncio.run(bili_api.fetch_user_card(q))
                return {"candidates": [{**card, "sign": ""}]}
            except bili_api.BiliApiError as e:
                if e.code == -404:
                    raise HTTPException(status_code=404,
                                        detail=f"未找到 UID 为 {q} 的用户")
                # card 接口被风控等情况下退回关键词搜索
        return {"candidates": asyncio.run(bili_api.search_users(q))}
    except HTTPException:
        raise
    except bili_api.BiliApiError as e:
        raise HTTPException(status_code=502,
                            detail=f"B站接口异常({e.code}): {e.message or '请稍后再试'}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"B站接口异常: {e}")


@app.post("/api/uppers")
def add_upper(req: AddUpperReq):
    """加入订阅后补齐可访问的历史内容，完成后切换增量采集。"""
    uid = req.uid.strip()
    if not _UID_RE.match(uid):
        raise HTTPException(status_code=400, detail="UID 必须是纯数字")

    name = req.name.strip()
    face = None
    try:
        card = asyncio.run(bili_api.fetch_user_card(uid))
        name = card.get("name") or name
        face = card.get("face")
    except bili_api.BiliApiError as e:
        if e.code == -404:
            raise HTTPException(status_code=404,
                                detail=f"UID {uid} 在B站不存在，请检查后重试")
        if not name:
            raise HTTPException(
                status_code=502,
                detail=f"无法从B站确认该 UID({e.code})，请稍后再试或改用搜索选择",
            )
    if not name:
        raise HTTPException(status_code=400, detail="缺少 UP 主昵称")

    if not db.add_upper(uid, name, face):
        return {"ok": True, "already": True, "message": f"{name} 已在订阅中"}

    if config.CLOUD_MODE:
        if config.CLOUD_TEST_MODE:
            return {"ok": True, "already": False, "message": f"已订阅 {name}；测试模式请手动采集"}
        return cloud.enqueue("crawl", uid, {"full_history": True})

    # 登录态失效时后台抓取会被跳过，提示语要说实话，别让用户以为已经在抓了
    login_ok = not crawler_runner.STATE.get("login_required", False)
    crawl_started = crawler_runner.start_single_bg(uid)
    if crawl_started and login_ok:
        msg = f"已订阅 {name}，正在后台补齐其历史内容"
    elif crawl_started:
        msg = f"已订阅 {name}，但B站登录态已失效、暂未抓取内容，请先在顶栏「扫码登录」"
    else:
        msg = f"已订阅 {name}，将随下一轮爬取抓取其内容"
    return {"ok": True, "already": False, "crawl_started": crawl_started,
            "login_required": not login_ok, "message": msg}


@app.delete("/api/uppers/{uid}")
def remove_upper(uid: str):
    """取消订阅（保留其历史数据，重新添加即恢复）。"""
    if db.remove_upper(uid):
        return {"ok": True, "message": "已取消订阅"}
    raise HTTPException(status_code=404, detail="该 UP 主不在订阅中")


@app.get("/api/uppers/{uid}/videos")
def get_videos(uid: str, limit: int = Query(30, ge=1, le=200), offset: int = Query(0, ge=0)):
    return db.list_videos(uid, limit=limit, offset=offset)


@app.get("/api/uppers/{uid}/dynamics")
def get_dynamics(uid: str, limit: int = Query(30, ge=1, le=200), offset: int = Query(0, ge=0)):
    return db.list_dynamics(uid, limit=limit, offset=offset)


@app.get("/api/summaries")
def get_summaries(date: str | None = Query(None)):
    return db.list_summaries(date=date)


@app.get("/api/research")
def get_research(request: Request, uid: str | None = Query(None, pattern=r"^\d{3,}$"), topic: str | None = Query(None, max_length=80)):
    state = finance_analysis.status()
    if config.CLOUD_MODE:
        queued = [job for job in cloud.jobs() if job["kind"] == "analyze" and job["status"] in ("queued", "running")]
        state.update(running=bool(queued), automatic=not config.CLOUD_TEST_MODE and config.FINANCE_AUTO_ANALYSIS)
        if not request.state.owner:
            state = {key: state[key] for key in ("running", "automatic")}
    return {"claims": finance_analysis.list_claims(uid, topic),
            "status": state, "evidence_jobs": finance_evidence.jobs(uid) if not config.CLOUD_MODE or request.state.owner else [],
            "cloud_jobs": cloud.jobs() if config.CLOUD_MODE and request.state.owner else []}


class AnalyzeReq(BaseModel):
    uid: str | None = Field(default=None, pattern=r"^\d{3,}$")


@app.post("/api/research/analyze")
def post_analysis(req: AnalyzeReq):
    if config.CLOUD_MODE:
        cloud.refresh_settings()
    if req.uid and req.uid not in db.subscribed_uids():
        raise HTTPException(404, "该UP主未订阅")
    if not llm_client.is_configured():
        raise HTTPException(400, "请先配置分析模型")
    if config.CLOUD_MODE:
        cloud.refresh_settings()
        if config.CLOUD_TEST_MODE and not req.uid:
            raise HTTPException(400, "测试阶段请先选择一位UP主")
        return cloud.enqueue("analyze", req.uid, {"per_upper": 1 if config.CLOUD_TEST_MODE else 4})
    started = finance_analysis.start_bg(req.uid)
    return {"ok": started, "message": "已启动增量分析" if started else "已有分析任务运行中，请稍后查看"}


class EvidenceReq(BaseModel):
    claim_id: int = Field(gt=0)
    url: str = Field(min_length=10, max_length=2000)


@app.post("/api/research/evidence")
def post_evidence(req: EvidenceReq):
    if config.CLOUD_MODE:
        cloud.refresh_settings()
    if not llm_client.is_configured():
        raise HTTPException(400, "请先配置分析模型")
    if config.CLOUD_MODE:
        try:
            finance_evidence.validate_url(req.url.strip())
        except ValueError as ex:
            raise HTTPException(400, str(ex)) from None
        claim = finance_analysis.get_claim(req.claim_id)
        if not claim or claim["uid"] not in db.subscribed_uids():
            raise HTTPException(404, "观点不存在")
        return cloud.enqueue("evidence", claim["uid"], {"claim_id": req.claim_id, "url": req.url.strip()})
    try:
        job = finance_evidence.start_bg(req.claim_id, req.url.strip())
    except ValueError as ex:
        raise HTTPException(400, str(ex))
    if job is None:
        raise HTTPException(409, "已有资料核验运行中，请稍后重试")
    return {"ok": True, "job_id": job, "message": "已开始读取资料并核验观点"}


@app.get("/api/status")
def get_status(request: Request):
    if config.CLOUD_MODE:
        state = cloud.load_state("crawl")
        jobs = cloud.jobs()
        active = [job for job in jobs if job["status"] in ("queued", "running")]
        authors = [{**u, **state.get("uppers", {}).get(u["uid"], {})} for u in db.subscribed_uppers()]
        if not request.state.owner:
            for author in authors:
                author.pop("last_error", None)
        result = {"uppers": authors,
                "crawling": any(j["kind"] in ("crawl", "scheduled") for j in active),
                "summarizing": any(j["kind"] == "summarize" for j in active),
                "login_required": state.get("login_required", False),
                "llm_configured": llm_client.is_configured(), "llm_model": config.LLM_MODEL,
                "cloud": True, "test_mode": config.CLOUD_TEST_MODE}
        if not request.state.owner:
            for key in ("login_required", "llm_configured", "llm_model", "test_mode"):
                result.pop(key, None)
        return result
    # 登录态缓存过期时后台复查一次（非阻塞），让登录失效在网页上及时可见，
    # 不必等到下一轮爬取才发现
    crawler_runner.maybe_refresh_login_state()
    return {
        "uppers": crawler_runner.get_status_uppers(),
        "crawling": crawler_runner.is_crawling(),
        "summarizing": summarizer.is_summarizing(),
        "login_required": crawler_runner.STATE.get("login_required", False),
        "login_checked_ts": crawler_runner.login_checked_ts(),
        "last_summary_error": crawler_runner.STATE.get("last_summary_error"),
        # 模型是否已配置：顶栏用它显示"未配置模型"提醒（不含任何密钥信息）
        "llm_configured": llm_client.is_configured(),
        "llm_model": config.LLM_MODEL,
    }


@app.get("/api/diagnostics")
def get_diagnostics():
    """一站式诊断快照（排错先看这个）。不含任何凭据值。"""
    return diagnostics.snapshot()


class CrawlReq(BaseModel):
    model_config = {"extra": "forbid"}
    uid: str | None = Field(default=None, pattern=r"^\d{3,}$")
    full_history: bool = Field(default=False, strict=True)


@app.post("/api/crawl")
def post_crawl(req: CrawlReq | None = None):
    if req and req.full_history and not req.uid:
        raise HTTPException(400, "请指定一位UP主补齐历史内容")
    if config.CLOUD_MODE:
        uid = req.uid if req else None
        if config.CLOUD_TEST_MODE and not uid:
            raise HTTPException(400, "测试阶段请在观点研究中选择一位UP主后采集")
        if uid and uid not in db.subscribed_uids():
            raise HTTPException(404, "该UP主未订阅")
        return cloud.enqueue("crawl", uid, {"full_history": True} if req and req.full_history else {})
    if req and req.uid:
        if req.full_history:
            crawler_runner.STATE["uppers"].setdefault(req.uid, {})["history"] = {"status": "pending"}
            crawler_runner._save_state()
        return {"ok": crawler_runner.start_single_bg(req.uid), "message": "已提交该UP主采集"}
    if not crawler_runner.start_full_round_bg():
        return {"ok": False, "message": "已在爬取中，请稍后再试"}
    return {"ok": True, "message": "已启动后台爬取任务"}


@app.post("/api/summarize")
def post_summarize():
    if config.CLOUD_MODE:
        return cloud.enqueue("summarize", payload={"period": "manual"})
    if not summarizer.start_generate_bg(period="manual"):
        return {"ok": False, "message": "已在生成总结中，请稍后再试"}
    return {"ok": True, "message": "已启动后台总结任务"}


# --- 扫码登录：生成二维码 / 轮询结果 / 检测登录态 ---

@app.post("/api/login/qrcode")
async def login_qrcode(request: Request):
    """生成 B站登录二维码（前端展示，用 B站 App 扫码）。"""
    try:
        result = await bili_login.create_qr()
        if config.CLOUD_MODE:
            cloud.register_qr(result["key"], request)
        return result
    except Exception as e:
        raise HTTPException(status_code=502, detail="二维码生成失败，请稍后重试") from None


class QrPollReq(BaseModel):
    model_config = {"extra": "forbid"}
    key: str = Field(pattern=r"^[A-Za-z0-9_-]{8,128}$")


@app.post("/api/login/qrcode/poll")
async def login_qrcode_poll(req: QrPollReq, request: Request):
    """轮询扫码状态；确认后登录态写入 .env 并即时生效（无需重启）。"""
    key = req.key
    if config.CLOUD_MODE:
        decision = cloud.begin_qr_poll(key, request)
        if decision == "invalid":
            raise HTTPException(403, "二维码已过期或不属于当前管理会话")
        if decision == "busy":
            raise HTTPException(409, "正在检查扫码结果，请稍后重试")
        if decision == "complete":
            return {"status": "confirmed", "message": "登录态已安全保存"}
    try:
        store = (lambda cookies: cloud.complete_qr_cookies(key, request, {env: cookies.get(name, "") for name, env in bili_login._COOKIE_TO_ENV.items()})) if config.CLOUD_MODE else None
        return await bili_login.poll_qr(key, store_cookies=store)
    except Exception as e:
        raise HTTPException(status_code=502, detail="扫码检查失败，请重新生成二维码") from None
    finally:
        if config.CLOUD_MODE:
            cloud.end_qr_poll(key, request)


@app.delete("/api/login")
def clear_login():
    if not config.CLOUD_MODE:
        raise HTTPException(404, "此操作仅适用于云端登录态")
    cloud.clear_bili_credentials()
    return {"ok": True, "message": "已清除本网站保存的B站登录态；需要重新扫码后才能采集"}


@app.get("/api/login/status")
async def login_status():
    """主动检测登录态是否有效。"""
    return await bili_login.check_status()


# Model credentials are deployment configuration, never webpage input.
class LlmTestReq(BaseModel):
    model_config = {"extra": "forbid"}


@app.get("/api/llm/status")
def llm_status():
    return {"configured": llm_client.is_configured(), "model": config.LLM_MODEL}


@app.post("/api/llm/test")
def llm_test(req: LlmTestReq):
    """Test the server's existing configuration; accept no credential overrides."""
    if not llm_client.is_configured():
        raise HTTPException(400, "分析服务未配置，请由管理员在部署平台配置")
    return llm_client.test_connection(
        config.LLM_API_KEY, config.LLM_BASE_URL, config.LLM_MODEL, thinking="disabled"
    )


# --- serve the built frontend (web/dist) so one process handles everything ---
# Must be registered after the /api routes: this catch-all resolves any
# non-API GET to a static file, falling back to the SPA entry point so
# react-router paths like /upper/123 work on direct page loads.
WEB_DIST = config.WORKSPACE_ROOT / "web" / "dist"


@app.get("/{full_path:path}", include_in_schema=False)
def spa_fallback(full_path: str):
    if full_path == "api" or full_path.startswith("api/"):
        raise HTTPException(404, "接口不存在")
    if config.CLOUD_API_ONLY:
        raise HTTPException(404, "页面由前端服务提供")
    if full_path:
        candidate = (WEB_DIST / full_path).resolve()
        try:
            candidate.relative_to(WEB_DIST.resolve())
        except ValueError:
            candidate = None
        if candidate is not None and candidate.is_file():
            return FileResponse(candidate)
    return FileResponse(WEB_DIST / "index.html")
