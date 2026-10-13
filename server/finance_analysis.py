"""Versioned, source-grounded finance analysis using the configured LLM.

Short SQLite transactions; no model call holds a database lock. One bounded
worker at a time, fair budgets per subscribed author, and failed-input backoff.
"""
import hashlib
import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

import config
import db
import llm_client
import summarizer
from finance_store import connect

log = logging.getLogger("finance_analysis")
VERSION = "finance-v1.0"
SKILLS = Path(__file__).parent / "analysis_skills"
_lock = threading.Lock()
_running = False
_progress = {"processed": 0, "failed": 0, "current_author": "", "started_ts": None}


class Change(BaseModel):
    previous_id: int
    kind: Literal["新增论据", "条件变化", "期限变化", "立场变化", "表述变化", "延续判断"]
    summary: str = Field(min_length=1, max_length=500)


class Claim(BaseModel):
    topic: str = Field(min_length=1, max_length=80)
    subject: str = Field(min_length=1, max_length=100)
    market: Literal["A股", "港股", "美股", "跨市场", "未明确", "其他"] = "未明确"
    kind: Literal["产业判断", "公司经营", "行情节奏", "参与条件", "宏观判断", "风险提示"]
    stance: Literal["看多", "看空", "中性", "不明确"] = "不明确"
    horizon: str = Field(default="未明确", max_length=200)
    claim: str = Field(min_length=1, max_length=500)
    quote: str = Field(min_length=10, max_length=500)
    rationale: list[str] = Field(default_factory=list, max_length=5)
    checks: list[str] = Field(default_factory=list, max_length=5)
    change: Change | None = None


class Extraction(BaseModel):
    claims: list[Claim] = Field(default_factory=list, max_length=5)


def init_store():
    with connect() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS finance_sources (
          id INTEGER PRIMARY KEY, uid TEXT NOT NULL, kind TEXT NOT NULL,
          source_id TEXT NOT NULL, input_hash TEXT NOT NULL, version TEXT NOT NULL,
          model TEXT NOT NULL, pub_ts INTEGER NOT NULL, title TEXT, url TEXT,
          text TEXT NOT NULL, created_ts INTEGER NOT NULL, truncated INTEGER NOT NULL,
          UNIQUE(uid,kind,source_id,input_hash,version));
        CREATE TABLE IF NOT EXISTS finance_claims (
          id INTEGER PRIMARY KEY, source INTEGER NOT NULL, uid TEXT NOT NULL,
          pub_ts INTEGER NOT NULL, topic TEXT NOT NULL, data TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS finance_claims_author ON finance_claims(uid,pub_ts);
        CREATE TABLE IF NOT EXISTS finance_attempts (
          input_hash TEXT PRIMARY KEY, failed_ts INTEGER, error TEXT, failures INTEGER);
        CREATE TABLE IF NOT EXISTS finance_runs (
          id INTEGER PRIMARY KEY, uid TEXT, started_ts INTEGER, finished_ts INTEGER,
          processed INTEGER DEFAULT 0, failed INTEGER DEFAULT 0, status TEXT);
        CREATE TABLE IF NOT EXISTS finance_evidence (
          id INTEGER PRIMARY KEY, claim_id INTEGER NOT NULL, url TEXT NOT NULL,
          text_hash TEXT NOT NULL, model TEXT NOT NULL, created_ts INTEGER NOT NULL,
          source_label TEXT NOT NULL, data TEXT NOT NULL,
          UNIQUE(claim_id,url,text_hash,model));
        """)
        if not config.CLOUD_MODE:
            c.execute("UPDATE finance_runs SET status='interrupted',finished_ts=? WHERE status='running'", (int(time.time()),))


def parse_json(content):
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content)
    return json.loads(content)


def call_json(system, payload):
    # Reuse the actual provider/model configuration and its existing retry rules.
    content, model = summarizer._call_llm(system + "\n必须输出JSON。", json.dumps(payload, ensure_ascii=False))
    return parse_json(content), model


def locate_quote(text, quote):
    """Allow whitespace differences, while storing the actual source substring."""
    direct = text.find(quote)
    if direct >= 0:
        return direct, direct + len(quote)
    indices = [i for i, ch in enumerate(text) if not ch.isspace()]
    compact = "".join(text[i] for i in indices)
    needle = "".join(ch for ch in quote if not ch.isspace())
    start = compact.find(needle)
    if start < 0 or not needle:
        raise ValueError("模型引用无法在原文定位，结果未保存")
    return indices[start], indices[start + len(needle) - 1] + 1


def candidates(uid, limit=40):
    with connect() as c:
        videos = [dict(r) for r in c.execute(
            "SELECT bvid source_id,title,url,pub_ts,subtitle text,subtitle_segments FROM videos WHERE uid=? AND length(subtitle)>=50 ORDER BY pub_ts DESC LIMIT ?", (uid, limit))]
        dynamics = [dict(r) for r in c.execute(
            "SELECT dyn_id source_id,'动态' title,url,pub_ts,text FROM dynamics WHERE uid=? AND length(text)>=35 ORDER BY pub_ts DESC LIMIT ?", (uid, limit))]
    for row in videos:
        row["kind"] = "video"
    for row in dynamics:
        row["kind"] = "dynamic"
    # Video-share wrappers repeat the title and contain no author's own argument.
    dynamics = [r for r in dynamics if not _share_wrapper(r["text"])]
    return sorted(videos + dynamics, key=lambda r: (r["pub_ts"], r["source_id"]), reverse=True)[:limit]


def _share_wrapper(text):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if lines and lines[0].startswith("[视频]"):
        title = lines[0][4:].strip()
        return len(lines) == 1 or all(line == title for line in lines[1:])
    return False


def fingerprint(source):
    material = [VERSION, config.LLM_MODEL, config.LLM_BASE_URL, source["kind"], source["source_id"], source.get("title"), source.get("pub_ts"), source["text"]]
    return hashlib.sha256(json.dumps(material, ensure_ascii=False).encode()).hexdigest()


def _done_or_cooling(input_hash):
    with connect() as c:
        if c.execute("SELECT 1 FROM finance_sources WHERE input_hash=? AND version=?", (input_hash, VERSION)).fetchone():
            return True
        row = c.execute("SELECT failed_ts,failures FROM finance_attempts WHERE input_hash=?", (input_hash,)).fetchone()
    return bool(row and int(time.time()) - row["failed_ts"] < min(21600, 300 * 2 ** min(row["failures"], 6)))


def _prior(uid, published):
    with connect() as c:
        rows = c.execute("SELECT id,data,pub_ts FROM finance_claims WHERE uid=? AND pub_ts<? ORDER BY pub_ts DESC,id DESC LIMIT 20", (uid, published)).fetchall()
    return {r["id"]: {**json.loads(r["data"]), "id": r["id"], "pub_ts": r["pub_ts"]} for r in rows}


def _timecode(source, start):
    if source["kind"] != "video" or not source.get("subtitle_segments"):
        return None
    try:
        segments = json.loads(source["subtitle_segments"])
        # The source text was joined without added separators. Do not estimate
        # seconds from characters when old sources have no timing information.
        if not source["text"].startswith("".join(s["text"] for s in segments)[:len(source["text"])]):
            return None
        offset = 0
        for segment in segments:
            if offset <= start < offset + len(segment["text"]):
                value = segment.get("start")
                return max(0, int(float(value))) if value is not None else None
            offset += len(segment["text"])
    except (TypeError, ValueError, KeyError):
        pass
    return None


def analyze_source(uid, source):
    digest = fingerprint(source)
    prior = _prior(uid, source["pub_ts"])
    text = source["text"][:20000]
    system = (SKILLS / "观点提取.md").read_text("utf-8") + "\n" + (SKILLS / "变化跟踪.md").read_text("utf-8")
    result, model = call_json(system, {
        "source": {"type": source["kind"], "published_ts": source["pub_ts"], "title": source["title"], "text": text},
        "history": [{k: r.get(k) for k in ("id", "pub_ts", "topic", "subject", "kind", "stance", "horizon", "claim", "quote")} for r in prior.values()],
        "schema": Extraction.model_json_schema(),
    })
    extracted = Extraction.model_validate(result)
    payloads = []
    for claim in extracted.claims:
        start, end = locate_quote(text, claim.quote)
        data = claim.model_dump()
        data.update(quote=text[start:end], quote_start=start, quote_end=end,
                    video_seconds=_timecode(source, start), evidence_status="作者观点，未独立核验")
        change = data.get("change")
        if change:
            previous = prior.get(change["previous_id"])
            if not previous:
                raise ValueError("模型引用了不存在或非历史的观点")
            _calibrate_change(data, previous)
        payloads.append(data)
    with connect() as c:
        cursor = c.execute("INSERT INTO finance_sources(uid,kind,source_id,input_hash,version,model,pub_ts,title,url,text,created_ts,truncated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
            uid, source["kind"], source["source_id"], digest, VERSION, model, source["pub_ts"], source["title"], source["url"], text, int(time.time()), int(len(source["text"]) > len(text))))
        for payload in payloads:
            c.execute("INSERT INTO finance_claims(source,uid,pub_ts,topic,data) VALUES(?,?,?,?,?)", (cursor.lastrowid, uid, source["pub_ts"], payload["topic"], json.dumps(payload, ensure_ascii=False)))
        c.execute("DELETE FROM finance_attempts WHERE input_hash=?", (digest,))
    return len(payloads)


def _calibrate_change(current, previous):
    change = current.get("change")
    if change and change["kind"] == "立场变化" and {previous["stance"], current["stance"]} != {"看多", "看空"}:
        change["kind"] = "表述变化"
        change["summary"] = "两条材料的表达重心或明确程度不同，暂不能据此认定作者实际立场改变；请对照前后原文。"


def run_batch(uid=None, per_upper=4):
    _progress.update(processed=0, failed=0, current_author="", started_ts=int(time.time()))
    with connect() as c:
        run_id = c.execute("INSERT INTO finance_runs(uid,started_ts,status) VALUES(?,?,'running')", (uid, int(time.time()))).lastrowid
    try:
        for upper in db.subscribed_uppers():
            if uid and str(upper["uid"]) != str(uid):
                continue
            author = str(upper["uid"])
            _progress["current_author"] = upper["name"]
            selected = [s for s in candidates(author) if not _done_or_cooling(fingerprint(s))][:per_upper]
            for source in reversed(selected):
                try:
                    analyze_source(author, source)
                    _progress["processed"] += 1
                except Exception as ex:
                    # Never persist SDK error bodies, endpoints or credentials.
                    message = str(ex)[:250] if isinstance(ex, ValueError) else "模型请求失败，请检查模型设置或稍后重试"
                    _progress["failed"] += 1
                    log.warning("analysis failed uid=%s source=%s type=%s", author, source["source_id"], type(ex).__name__)
                    with connect() as c:
                        c.execute("INSERT INTO finance_attempts VALUES(?,?,?,1) ON CONFLICT(input_hash) DO UPDATE SET failed_ts=excluded.failed_ts,error=excluded.error,failures=finance_attempts.failures+1", (fingerprint(source), int(time.time()), message))
        return dict(_progress)
    finally:
        with connect() as c:
            c.execute("UPDATE finance_runs SET finished_ts=?,processed=?,failed=?,status=? WHERE id=?", (int(time.time()), _progress["processed"], _progress["failed"], "partial" if _progress["failed"] else "complete", run_id))
        _progress["current_author"] = ""


def start_bg(uid=None, per_upper=4):
    global _running
    if not llm_client.is_configured():
        return False
    with _lock:
        if _running:
            return False
        _running = True
        _progress.update(processed=0, failed=0, current_author="", started_ts=int(time.time()))
    def worker():
        global _running
        try:
            run_batch(uid, per_upper)
        except Exception:
            log.exception("finance batch failed")
        finally:
            with _lock:
                _running = False
    threading.Thread(target=worker, daemon=True, name="finance-analysis").start()
    return True


def status():
    with connect() as c:
        last = c.execute("SELECT * FROM finance_runs ORDER BY id DESC LIMIT 1").fetchone()
        failures = c.execute("SELECT error,failed_ts FROM finance_attempts ORDER BY failed_ts DESC LIMIT 5").fetchall()
    return {"running": _running, "automatic": config.FINANCE_AUTO_ANALYSIS, **_progress, "last_run": dict(last) if last else None,
            "errors": [dict(r) for r in failures], "configured": llm_client.is_configured()}


def list_claims(uid=None, topic=None, limit=150):
    args = []
    where = ["u.subscribed=1", "s.id=(SELECT max(v.id) FROM finance_sources v WHERE v.uid=s.uid AND v.kind=s.kind AND v.source_id=s.source_id)"]
    if uid:
        where.append("f.uid=?")
        args.append(uid)
    if topic:
        where.append("f.topic=?")
        args.append(topic)
    with connect() as c:
        rows = c.execute("SELECT f.*,u.name author,s.title,s.url,s.kind source_kind,s.model,s.truncated,s.created_ts FROM finance_claims f JOIN finance_sources s ON s.id=f.source JOIN uppers u ON u.uid=f.uid WHERE " + " AND ".join(where) + " ORDER BY f.pub_ts DESC,f.id DESC LIMIT ?", args + [limit]).fetchall()
        payloads = {row["id"]: json.loads(row["data"]) for row in rows}
        previous_ids = sorted({payload["change"]["previous_id"] for payload in payloads.values() if payload.get("change")})
        previous_rows = {}
        if previous_ids:
            placeholders = ",".join("?" for _ in previous_ids)
            previous_rows = {(row["id"], row["uid"]): row for row in c.execute("SELECT f.id,f.uid,f.pub_ts,f.data,s.url FROM finance_claims f JOIN finance_sources s ON s.id=f.source WHERE f.id IN (" + placeholders + ")", previous_ids)}
        evidence_rows = {}
        if rows:
            claim_ids = [row["id"] for row in rows]
            placeholders = ",".join("?" for _ in claim_ids)
            for evidence in c.execute("SELECT claim_id,id,url,created_ts,source_label,data FROM finance_evidence WHERE claim_id IN (" + placeholders + ") ORDER BY id DESC", claim_ids):
                item = dict(evidence)
                claim_id = item.pop("claim_id")
                evidence_rows.setdefault(claim_id, []).append({**item, **json.loads(item["data"])})
        output = []
        for row in rows:
            data = {**dict(row), **payloads[row["id"]]}
            data.pop("data")
            change = data.get("change")
            if change:
                previous = previous_rows.get((change["previous_id"], row["uid"]))
                if previous:
                    data["previous"] = {"id": previous["id"], "pub_ts": previous["pub_ts"], "url": previous["url"], **json.loads(previous["data"])}
                    _calibrate_change(data, data["previous"])
            data["evidence"] = evidence_rows.get(row["id"], [])
            output.append(data)
    return output


def get_claim(claim_id):
    with connect() as c:
        row = c.execute("SELECT f.*,s.text,s.pub_ts,s.url FROM finance_claims f JOIN finance_sources s ON s.id=f.source WHERE f.id=?", (claim_id,)).fetchone()
    return {**dict(row), **json.loads(row["data"])} if row else None
